"""
运行状态管理 —— 把"同步的 Agent 循环"和"异步的 Web 服务"接起来。

===========================================================================
核心问题：Agent 是同步阻塞的，Web 是异步的
===========================================================================

Agent 循环长这样：调模型（等几秒）→ 调工具（等几百毫秒）→ 再调模型……
全程是**阻塞**的。一次诊断可能跑 30~60 秒。

而 FastAPI 跑在 asyncio 事件循环上。如果直接在路由函数里 `await agent.run()`：

  1. 用 `await` 调一个同步阻塞函数是**假的异步** —— 它会把整个事件循环
     卡住，所有其它请求（包括你自己的 SSE 连接）全都得排队
  2. 一个诊断把服务堵死 60 秒，这不是"慢"，是"服务不可用"

正确做法是把 Agent 扔到**工作线程**里跑，事件通过线程安全的通道送回事件循环：

    asyncio 事件循环                    工作线程
    ─────────────────                  ────────────────
    接收请求
      └─ asyncio.to_thread ──────────►  agent.run()
                                          │ on_event 回调
    订阅者队列  ◄── call_soon_threadsafe ─┘

**这里是整个 Web 层唯一需要小心的地方。** `call_soon_threadsafe` 是必须的 ——
直接往 asyncio.Queue 里 put 是未定义行为，因为那不是在事件循环线程里做的。

===========================================================================
第二个设计：事件要能"回放"，否则 EventSource 的重连会重跑诊断
===========================================================================

浏览器原生的 EventSource **断线后会自动重连**。如果服务端把事件当成一次性流：

    连接断了 → 浏览器自动重连 → 服务端又开始一次新诊断 → 无限循环烧钱

所以 Run 保存了**完整的事件历史**，并且订阅时从指定序号开始回放：

    连接断了 → 浏览器重连（带上 Last-Event-ID）→ 从断点继续发 → 无缝衔接

SSE 协议本身就支持这个：每条消息带一个 `id:` 字段，浏览器断线重连时会
自动把它放进 `Last-Event-ID` 请求头。**这是 SSE 相比 WebSocket 少有人用的
一个优势** —— 断点续传是协议内置的，不用自己实现。
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

# ---------------------------------------------------------------------------
# 运行历史持久化
# ---------------------------------------------------------------------------
# 默认不持久化；设了 NETPULSE_RUNS_DIR 才落盘（每 run 一个 .meta.json + .events.jsonl）。
# 之所以做成"可关"，是不想给测试 / 一次性场景引入磁盘副作用；Docker / 原生长跑才开。
#
# 设计取舍（和 agent/memory.py 一致）：JSONL 是事实源，可读、可改、可删；
# 这里同理——每个 run 的轨迹都能直接打开看，记错了/不想要了删两个文件即可。
#
# 持久化的是"run 的完整事件轨迹 + 元信息"，因此重启后能：
#   · 左侧历史侧栏继续显示过往诊断
#   · 点任意一条用 SSE 回放（since=0 重放 self.events，和在线时完全一致）
# 未完成的 run（服务中途崩了）在加载时会被丢弃，避免回放时卡在等 done。
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_raw_runs_dir = os.environ.get("NETPULSE_RUNS_DIR", "").strip()
RUNS_DIR = _raw_runs_dir if _raw_runs_dir.lower() not in ("", "off", "disabled", "false", "0") else ""
_PERSIST_ENABLED = bool(RUNS_DIR)
_persist_lock = threading.Lock()


def _meta_path(run_id: str) -> str:
    return os.path.join(RUNS_DIR, run_id + ".meta.json")


def _evt_path(run_id: str) -> str:
    return os.path.join(RUNS_DIR, run_id + ".events.jsonl")


def _persist_run_start(run: "Run") -> None:
    if not _PERSIST_ENABLED:
        return
    try:
        os.makedirs(RUNS_DIR, exist_ok=True)
        with _persist_lock, open(_meta_path(run.run_id), "w", encoding="utf-8") as f:
            json.dump({
                "run_id": run.run_id, "question": run.question,
                "started_at": run.started_at, "done": False,
                "error": None, "result": None, "finished_at": None,
            }, f, ensure_ascii=False)
    except Exception:
        pass  # 持久化失败绝不能影响主流程


def _persist_run_event(run: "Run", event: "RunEvent") -> None:
    if not _PERSIST_ENABLED:
        return
    try:
        os.makedirs(RUNS_DIR, exist_ok=True)
        with _persist_lock, open(_evt_path(run.run_id), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "seq": event.seq, "kind": event.kind, "payload": event.payload,
                "elapsed_ms": event.elapsed_ms, "ts": time.time(),
            }, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _persist_run_end(run: "Run") -> None:
    if not _PERSIST_ENABLED:
        return
    try:
        with _persist_lock, open(_meta_path(run.run_id), "w", encoding="utf-8") as f:
            json.dump({
                "run_id": run.run_id, "question": run.question,
                "started_at": run.started_at, "done": run.done, "error": run.error,
                "result": run.result, "finished_at": run.finished_at,
            }, f, ensure_ascii=False, default=str)
    except Exception:
        pass


def _drop_run_files(run_id: str) -> None:
    if not _PERSIST_ENABLED:
        return
    for p in (_meta_path(run_id), _evt_path(run_id)):
        try:
            os.remove(p)
        except OSError:
            pass


@dataclass
class RunEvent:
    """一条事件。seq 是单调递增的序号，用作 SSE 的 id。"""

    seq: int
    kind: str
    payload: dict[str, Any]
    elapsed_ms: float

    def to_sse(self) -> str:
        """渲染成 SSE 帧。

        格式（注意每个字段后面都要有空行，最后再来一个空行表示结束）：

            id: 3
            event: tool_result
            data: {"name": "ping_host", ...}

        `event:` 那一行是必需的 —— 前端用 `addEventListener('tool_result', ...)`
        按类型分发，比在回调里 switch `data.kind` 干净得多。
        """
        import json

        data = json.dumps(self.payload, ensure_ascii=False, default=str)
        return (
            f"id: {self.seq}\n"
            f"event: {self.kind}\n"
            f"data: {data}\n\n"
        )


class Run:
    """一次诊断的运行状态。可以被多个订阅者同时消费。"""

    def __init__(self, question: str, run_id: str | None = None) -> None:
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        self.question = question
        self.events: list[RunEvent] = []
        self.done = False
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None

        self._subscribers: set[asyncio.Queue[RunEvent]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记住事件循环，之后才能从别的线程往里投递。"""
        self._loop = loop

    def push(self, kind: str, payload: dict[str, Any]) -> None:
        """**从工作线程调用**。这是 Agent 的 on_event 回调。"""
        event = RunEvent(
            seq=len(self.events),
            kind=kind,
            payload=payload,
            elapsed_ms=round((time.time() - self.started_at) * 1000, 1),
        )
        self.events.append(event)
        _persist_run_event(self, event)

        # 关键：必须用 call_soon_threadsafe。
        # asyncio.Queue 不是线程安全的，从别的线程直接 put_nowait
        # 在单线程下"看起来能跑"，但在真实并发下会丢事件或者更糟 ——
        # 而且它不会立刻报错，是最难查的那类 bug。
        if self._loop is not None and not self._loop.is_closed():
            for queue in list(self._subscribers):
                try:
                    self._loop.call_soon_threadsafe(self._offer, queue, event)
                except RuntimeError:
                    pass  # 循环已经关了（服务正在退出），忽略

    @staticmethod
    def _offer(queue: asyncio.Queue[RunEvent], event: RunEvent) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # 订阅者消费不过来（比如浏览器标签页卡住了）。
            # 丢事件比让服务端内存涨爆好 —— 而且前端断线重连时会走回放补齐。
            pass

    def finish(self, result: dict[str, Any]) -> None:
        self.result = result
        self.done = True
        self.finished_at = time.time()
        self.push("done", {"run_id": self.run_id, "elapsed_ms": self.elapsed_ms})
        _persist_run_end(self)

    def fail(self, message: str) -> None:
        self.error = message
        self.done = True
        self.finished_at = time.time()
        self.push("done", {"run_id": self.run_id, "error": message,
                           "elapsed_ms": self.elapsed_ms})
        _persist_run_end(self)

    @property
    def elapsed_ms(self) -> float:
        end = self.finished_at or time.time()
        return round((end - self.started_at) * 1000, 1)

    # ------------------------------------------------------------------

    async def subscribe(self, since: int = 0) -> AsyncIterator[RunEvent]:
        """订阅事件流，从 seq >= since 开始。

        顺序很重要：**先注册订阅者，再回放历史。**
        反过来的话，在"读完历史"和"注册订阅"之间产生的事件会永久丢失 ——
        而这正是断线重连时最容易踩到的窗口。
        """
        queue: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=1024)
        self._subscribers.add(queue)
        try:
            sent = since
            # 回放历史。此刻已经订阅了，所以这之后的新事件会进队列，
            # 下面收到 seq < sent 的丢掉即可（队列里可能有回放期间重复的）。
            while sent < len(self.events):
                yield self.events[sent]
                sent += 1

            if self.done:
                return

            while True:
                event = await queue.get()
                if event.seq < sent:
                    continue  # 回放期间已经发过了
                sent = event.seq + 1
                yield event
                if event.kind == "done":
                    return
        finally:
            self._subscribers.discard(queue)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "question": self.question,
            "done": self.done,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "event_count": len(self.events),
            "result": self.result,
        }


class RunStore:
    """所有运行记录的容器。带容量上限，避免长时间运行内存无限涨。"""

    def __init__(self, max_runs: int = 50) -> None:
        self._runs: dict[str, Run] = {}
        self._order: list[str] = []
        self.max_runs = max_runs
        self._load_persisted()

    def _load_persisted(self) -> None:
        """启动时扫描 RUNS_DIR，把已完成 run 重建回内存。

        未完成（done=False）的 run 是"服务中途崩了"留下的残骸 —— 直接丢弃
        并删文件，避免回放时卡在等 done 永远转圈。

        seq 必须连续（0,1,2,...），subscribe() 回放依赖这一点；落盘时已经
        按 push 顺序逐条 append，所以重新读回来天然连续，不需额外对齐。
        """
        if not _PERSIST_ENABLED:
            return
        try:
            files = sorted(glob.glob(os.path.join(RUNS_DIR, "*.meta.json")))
        except OSError:
            return

        for meta_path in files:
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError):
                _drop_run_files(os.path.basename(meta_path)[: -len(".meta.json")])
                continue

            run_id = meta.get("run_id")
            if not run_id:
                continue

            if not meta.get("done"):
                # 残骸：删掉（meta + events 两个文件）
                _drop_run_files(run_id)
                continue

            # 重建 Run 对象，但 _loop 留空 —— 重启后没有在飞的订阅者，
            # 历史 run 只会被回放（subscribe 走事件列表分支），不会被推送。
            run = Run(meta.get("question", ""), run_id=run_id)
            run.done = True
            run.result = meta.get("result")
            run.error = meta.get("error")
            run.started_at = meta.get("started_at", time.time())
            run.finished_at = meta.get("finished_at")

            # 逐行读 events.jsonl 填回 self.events（seq 用作真实序号）
            evt_path = _evt_path(run_id)
            try:
                with open(evt_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        run.events.append(RunEvent(
                            seq=rec.get("seq", len(run.events)),
                            kind=rec.get("kind", "unknown"),
                            payload=rec.get("payload", {}),
                            elapsed_ms=rec.get("elapsed_ms", 0.0),
                        ))
            except OSError:
                pass

            self._runs[run_id] = run
            self._order.append(run_id)

    def create(self, question: str) -> Run:
        run = Run(question)
        self._runs[run.run_id] = run
        self._order.append(run.run_id)
        _persist_run_start(run)

        # 淘汰最旧的。注意：**只淘汰已经结束的** ——
        # 正在跑的 Run 被淘汰的话，订阅它的 SSE 会永远等不到 done，
        # 前端就一直转圈。
        #
        # 代价是：如果很多诊断同时在飞，条数会短暂超过 max_runs
        # （循环遇到第一个未结束的就停）。这是刻意的取舍 ——
        # 在飞的诊断数量本来就有上限（受线程池限制），内存不会失控；
        # 而淘汰一个在飞的 Run 是**功能损坏**，不是内存优化。
        while len(self._order) > self.max_runs:
            oldest = self._order[0]
            if not self._runs[oldest].done:
                break
            self._order.pop(0)
            dropped = self._runs.pop(oldest, None)
            if dropped is not None:
                _drop_run_files(oldest)
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近的运行摘要（不含完整事件，给列表页用）。"""
        out = []
        for rid in reversed(self._order[-limit:]):
            run = self._runs.get(rid)
            if run is None:
                continue
            out.append(
                {
                    "run_id": run.run_id,
                    "question": run.question,
                    "done": run.done,
                    "error": run.error,
                    "elapsed_ms": run.elapsed_ms,
                    "rounds": (run.result or {}).get("rounds"),
                    "tool_calls": (run.result or {}).get("tool_calls"),
                }
            )
        return out

    def __len__(self) -> int:
        return len(self._runs)
