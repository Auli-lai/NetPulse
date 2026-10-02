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
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


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

    def fail(self, message: str) -> None:
        self.error = message
        self.done = True
        self.finished_at = time.time()
        self.push("done", {"run_id": self.run_id, "error": message,
                           "elapsed_ms": self.elapsed_ms})

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

    def create(self, question: str) -> Run:
        run = Run(question)
        self._runs[run.run_id] = run
        self._order.append(run.run_id)

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
            self._runs.pop(oldest, None)
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
