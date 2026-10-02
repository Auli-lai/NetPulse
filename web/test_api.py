"""
Web 服务测试 —— 全部离线，不需要 API Key / D-Bus / 真的起服务器。

    python test_api.py

===========================================================================
测什么（以及为什么不是"把接口都点一遍"）
===========================================================================

把每个 endpoint 都请求一遍，只能证明"没崩"。真正容易错的是下面几类：

  1. **线程 → 事件循环的桥接**。Agent 在工作线程里跑，事件要用
     `call_soon_threadsafe` 投回事件循环。写错的话（直接 put_nowait）
     在单线程测试里**看起来完全正常**，只在真实并发下丢事件。
     所以这里专门测"在别的线程 push，订阅方能不能收到"。

  2. **SSE 断点续传**。EventSource 断线会自动重连，服务端必须能
     从指定序号续发。做错了后果很严重：重连 = 重跑一次诊断 = 重复烧钱。
     而且这个 bug 在"不模拟重连"的测试里根本看不出来。

  3. **多订阅者**。两个浏览器标签页看同一次诊断，两边都该收到全部事件。

  4. **失败也要有终点**。诊断抛异常时，订阅方必须收到 done ——
     否则前端会一直转圈等一个永远不来的事件。

前三条都是**在正常路径下看不出来**的问题，这才是测试该覆盖的地方。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time

os.environ.setdefault("RAG_OFFLINE", "1")  # 用离线向量，不调 embedding API

from fastapi.testclient import TestClient  # noqa: E402

import app as webapp  # noqa: E402
from runstore import Run, RunStore  # noqa: E402

_PASSED = 0
_FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [OK] {name}")
    else:
        _FAILED += 1
        print(f"  [FAIL] {name}")
        if detail:
            for line in str(detail).splitlines()[:6]:
                print(f"         {line}")


def parse_sse(response) -> list[tuple[str, dict, int]]:
    """把 SSE 流解析成 [(event, data, id), ...]。"""
    out: list[tuple[str, dict, int]] = []
    event, data, eid = None, None, -1
    for line in response.iter_lines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line.split(":", 1)[1].strip())
            except json.JSONDecodeError:
                data = {}
        elif line.startswith("id:"):
            eid = int(line.split(":", 1)[1].strip())
        elif line == "" and event:
            out.append((event, data or {}, eid))
            event, data, eid = None, None, -1
    return out


# ---------------------------------------------------------------------------
# 1. 基础接口
# ---------------------------------------------------------------------------


def test_basic_endpoints() -> None:
    print("\n[1] 基础接口")
    c = TestClient(webapp.app)

    r = c.get("/api/health")
    check("健康检查返回 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("健康检查有 checks 字段", "checks" in body, str(body)[:200])
    check("健康检查报告了各项依赖状态",
          all(k in body["checks"] for k in ("api_key", "rag_index", "dbus_python")),
          str(body.get("checks")))

    r = c.get("/api/tools")
    tools = r.json()
    check("工具接口返回 200", r.status_code == 200)
    check("工具数量与 agent/tools.py 一致", tools["count"] == len(webapp.ToolRunner().tools),
          f"{tools['count']}")
    check("每个工具都带 description",
          all(len(t["description"]) > 50 for t in tools["tools"]))

    r = c.get("/api/runs")
    check("运行列表返回 200", r.status_code == 200)

    # 健康检查必须报出"是哪个 Python 在跑" ——
    # 排查"明明装了却 import 不到"时，这是最缺的一条信息
    # （apt 装的包是给系统 Python 的，venv 看不到）
    check("健康检查报出了 Python 解释器路径",
          bool(body["checks"].get("python")), str(body["checks"])[:200])
    check("健康检查报出了 Python 版本",
          bool(body["checks"].get("python_version")), str(body["checks"])[:200])


def test_health_reports_real_reason() -> None:
    """健康检查必须报出**真正的原因**，不能吞掉错误。

    这里守的是一个真实踩过的坑：

        try:
            ToolRunner().run("list_interfaces", {})
            checks["netpulse_service"] = True     # ← 永远走这一支
        except Exception:
            checks["netpulse_service"] = False

    `ToolRunner.run()` 的设计就是**绝不抛异常**（错误包成 {"error": ...} 返回，
    好让模型自我修正）。所以 try/except 抓不到任何东西，探测恒为成功。
    加上前端把"没装 dbus-python"和"服务没起"归成一句话，
    结果就是：少装一个 apt 包，却提示用户"你的 C++ 服务没启动"，
    用户会去反复重启一个本来跑得好好的服务。

    判据必须是**看返回值里有没有 error**。
    """
    import types
    from unittest.mock import patch

    print("\n[1b] 健康检查：报原因而不是猜原因")

    class RunnerReturningError:
        def __init__(self, *a, **k): ...
        def run(self, name, arguments=None):
            # 模拟"dbus 装了但服务连不上"
            return {"error": "连不上 NetPulse 服务（com.example.WeakNet）"}

    fake_dbus = types.ModuleType("dbus")
    c = TestClient(webapp.app)

    with patch.dict(sys.modules, {"dbus": fake_dbus}), \
         patch.object(webapp, "ToolRunner", RunnerReturningError):
        body = c.get("/api/health").json()

    checks = body["checks"]
    check("dbus 可导入时报 dbus_python=True", checks.get("dbus_python") is True,
          str(checks))
    check("**服务连不上时 netpulse_service 必须是 False**（不能恒为 True）",
          checks.get("netpulse_service") is False, str(checks))
    # 断言"原文被带出来了"，而不是断言某个词 —— 排查靠的就是这段原文，
    # 它必须和底层返回的一模一样，不能被摘要掉
    check("带上了原始错误原文（逐字）",
          checks.get("netpulse_error") == "连不上 NetPulse 服务（com.example.WeakNet）",
          repr(checks.get("netpulse_error")))
    check("给出了 hints 修法", isinstance(body.get("hints"), list) and body["hints"],
          str(body.get("hints"))[:200])
    check("hints 提到了 sudo -E（最常见的真实原因）",
          any("sudo -E" in h for h in body.get("hints", [])),
          str(body.get("hints"))[:300])

    # 反过来：服务正常时不能误报
    class RunnerOK:
        def __init__(self, *a, **k): ...
        def run(self, name, arguments=None):
            return {"interfaces": ["eth0", "wlan0"]}

    with patch.dict(sys.modules, {"dbus": fake_dbus}), \
         patch.object(webapp, "ToolRunner", RunnerOK):
        body2 = c.get("/api/health").json()
    check("服务正常时 netpulse_service=True",
          body2["checks"].get("netpulse_service") is True, str(body2["checks"]))
    check("服务正常时不报 hints", not body2.get("hints"), str(body2.get("hints")))

    r = c.get("/")
    check("首页能取到", r.status_code == 200)
    check("首页是完整 HTML", "<!DOCTYPE html>" in r.text and "</html>" in r.text)

    # 零外部依赖（断网也能完整显示）。
    #
    # 判据是"有没有真正去**加载**外部资源"，也就是 src/href 指向 http(s)。
    # 不能简单地搜 "http://" —— favicon 里的
    # `xmlns='http://www.w3.org/2000/svg'` 是 XML **命名空间标识符**，
    # 浏览器从不发请求。第一版就是这么误报的。
    external = re.findall(r'(?:src|href)\s*=\s*["\']((?:https?:)?//[^"\']+)', r.text)
    check("首页不加载任何外部资源（可离线）", not external,
          f"发现外部引用：{external[:5]}")
    check("没有 CDN 依赖", "cdn." not in r.text.lower() and "unpkg" not in r.text.lower())
    check("没有外部字体", "fonts.googleapis" not in r.text)


# ---------------------------------------------------------------------------
# 2. 完整诊断链路
# ---------------------------------------------------------------------------


def test_full_diagnosis_cycle() -> None:
    print("\n[2] 完整链路：POST 发起 -> SSE 订阅 -> 拿结果")
    c = TestClient(webapp.app)

    r = c.post("/api/diagnose", json={"question": "网络很卡", "demo": True})
    check("发起诊断返回 200", r.status_code == 200, str(r.status_code))
    run_id = r.json()["run_id"]
    check("返回了 run_id", bool(run_id))

    with c.stream("GET", f"/api/diagnose/{run_id}/stream") as resp:
        check("SSE content-type 正确",
              resp.headers.get("content-type", "").startswith("text/event-stream"),
              resp.headers.get("content-type", ""))
        check("关了代理缓冲（X-Accel-Buffering）",
              resp.headers.get("x-accel-buffering") == "no",
              str(dict(resp.headers)))
        events = parse_sse(resp)

    kinds = [e for e, _, _ in events]
    check("收到 start", "start" in kinds, str(kinds))
    check("收到 thought", "thought" in kinds, str(kinds))
    check("收到 tool_result", "tool_result" in kinds, str(kinds))
    check("收到 final", "final" in kinds, str(kinds))
    check("收到 done", "done" in kinds, str(kinds))
    check("done 是最后一个事件", kinds[-1] == "done", str(kinds[-3:]))

    # 事件序号必须单调递增且连续 —— 前端靠它做断点续传
    seqs = [i for _, _, i in events if i >= 0]
    check("事件序号连续递增", seqs == list(range(len(seqs))), f"{seqs[:12]}…")

    # tool_result 必须带上参数和返回原文，否则前端画不出推理链
    tr = next(d for k, d, _ in events if k == "tool_result")
    check("tool_result 带 arguments", "arguments" in tr, str(sorted(tr.keys())))
    check("tool_result 带 raw 返回内容", bool(tr.get("raw")), str(sorted(tr.keys())))
    check("tool_result 带耗时", "elapsed_ms" in tr)

    time.sleep(0.2)
    r = c.get(f"/api/diagnose/{run_id}")
    body = r.json()
    check("结果接口返回完成状态", body["done"] is True, str(body)[:200])
    check("结果里有完整诊断", (body.get("result") or {}).get("answer"),
          str(body)[:200])

    r = c.get("/api/diagnose/no_such_run")
    check("不存在的 run 返回 404", r.status_code == 404, str(r.status_code))


# ---------------------------------------------------------------------------
# 3. SSE 断点续传（最重要的一项）
# ---------------------------------------------------------------------------


def test_sse_resume() -> None:
    print("\n[3] SSE 断点续传 —— 重连不能重跑诊断")
    c = TestClient(webapp.app)

    r = c.post("/api/diagnose", json={"question": "网络很卡", "demo": True})
    run_id = r.json()["run_id"]
    time.sleep(0.5)  # 让诊断先跑一会儿

    # 全量订阅
    with c.stream("GET", f"/api/diagnose/{run_id}/stream") as resp:
        all_events = parse_sse(resp)
    check("全量订阅拿到了事件", len(all_events) > 3, f"{len(all_events)} 条")

    # 从第 2 条之后续传（模拟浏览器带 Last-Event-ID: 2 重连）
    with c.stream("GET", f"/api/diagnose/{run_id}/stream",
                  headers={"Last-Event-ID": "2"}) as resp:
        resumed = parse_sse(resp)
    check("续传只拿到后面的", len(resumed) < len(all_events),
          f"全量 {len(all_events)} vs 续传 {len(resumed)}")

    # 用 ?since= 显式指定（curl 调试路径）
    with c.stream("GET", f"/api/diagnose/{run_id}/stream?since=3") as resp:
        manual = parse_sse(resp)
    seqs = [i for _, _, i in manual if i >= 0]
    check("?since=3 只返回序号 >= 3 的事件", all(s >= 3 for s in seqs), str(seqs[:10]))
    check("?since=3 的第一条就是 3", seqs and seqs[0] == 3, str(seqs[:5]))

    # Last-Event-ID 请求头（EventSource 自动带的那种）—— 语义是"最后收到的"，
    # 所以服务端应该从它 +1 开始发
    with c.stream("GET", f"/api/diagnose/{run_id}/stream",
                  headers={"Last-Event-ID": "4"}) as resp:
        via_header = parse_sse(resp)
    hseqs = [i for _, _, i in via_header if i >= 0]
    check("Last-Event-ID: 4 -> 从 5 开始（不重复发已收到的）",
          hseqs and hseqs[0] == 5, str(hseqs[:6]))
    check("重连不会产生新的事件（没重跑诊断）",
          len(hseqs) < len(all_events), f"{len(hseqs)} vs {len(all_events)}")


def test_multiple_subscribers() -> None:
    print("\n[3b] 两个订阅者看同一次诊断，都应该拿到完整事件")
    c = TestClient(webapp.app)

    run_id = c.post("/api/diagnose",
                    json={"question": "网络很卡", "demo": True}).json()["run_id"]
    time.sleep(0.4)

    with c.stream("GET", f"/api/diagnose/{run_id}/stream") as r1:
        a = parse_sse(r1)
    with c.stream("GET", f"/api/diagnose/{run_id}/stream") as r2:
        b = parse_sse(r2)

    check("两个订阅者拿到同样多的事件", len(a) == len(b), f"{len(a)} vs {len(b)}")
    check("两个订阅者的事件序列相同",
          [k for k, _, _ in a] == [k for k, _, _ in b])


# ---------------------------------------------------------------------------
# 4. RunStore 的线程桥接
# ---------------------------------------------------------------------------


def test_thread_bridge() -> None:
    """在**别的线程**里 push，订阅方要能收到。

    这是整条链路里唯一真正并发的地方。用 call_soon_threadsafe 才是对的；
    写成直接 put_nowait 的话，下面这个测试有时能过有时不过 ——
    而线上就是丢事件。
    """
    print("\n[4] 线程桥接：工作线程 push，事件循环里能收到")

    async def scenario() -> tuple[list[str], list[str]]:
        run = Run("测试问题")
        run.bind_loop(asyncio.get_running_loop())

        received: list[str] = []

        async def consume() -> None:
            async for ev in run.subscribe():
                received.append(ev.kind)

        task = asyncio.create_task(consume())

        def worker() -> None:
            # 模拟 Agent 在工作线程里跑
            for i in range(5):
                run.push("thought", {"round": i, "text": f"第 {i} 轮"})
                time.sleep(0.01)
            run.finish({"rounds": 5})

        await asyncio.to_thread(worker)
        await asyncio.wait_for(task, timeout=5)
        return received, [e.kind for e in run.events]

    kinds, stored = asyncio.run(scenario())
    check("订阅方收到了全部 5 个 thought", kinds.count("thought") == 5, str(kinds))
    check("订阅方收到了 done", "done" in kinds, str(kinds))
    check("存下来的和收到的一致", kinds == stored, f"收到 {kinds} / 存了 {stored}")


def test_failure_still_terminates() -> None:
    """诊断抛异常时订阅方必须收到 done，否则前端永远转圈。"""
    print("\n[4b] 诊断失败也必须给订阅方一个终点")

    async def scenario() -> list[str]:
        run = Run("会失败的问题")
        run.bind_loop(asyncio.get_running_loop())

        async def consume() -> list[str]:
            return [ev.kind async for ev in run.subscribe()]

        task = asyncio.create_task(consume())
        await asyncio.to_thread(lambda: run.fail("模拟崩溃"))
        return await asyncio.wait_for(task, timeout=5)

    kinds = asyncio.run(scenario())
    check("失败时也发出了 done", "done" in kinds, str(kinds))


def test_runstore_eviction() -> None:
    print("\n[5] 运行记录的容量控制")

    # 契约一：已结束的运行会被淘汰
    store = RunStore(max_runs=3)
    first = store.create("最早的").run_id
    store.get(first).finish({"rounds": 1})     # 标记为已结束
    for i in range(4):
        run = store.create(f"q{i}")
        run.finish({"rounds": 1})
    check("已结束的旧运行会被淘汰", store.get(first) is None,
          f"store 里还有 {len(store)} 条")

    # 契约二：**未结束的运行永远不淘汰**，哪怕已经超出上限。
    #
    # 这是刻意的取舍：淘汰一个正在跑的 Run，订阅它的 SSE 就永远等不到 done，
    # 前端会一直转圈。宁可让计数短暂超过上限（在飞的诊断本来就有限），
    # 也不能让一个客户端挂死。
    store2 = RunStore(max_runs=2)
    running = store2.create("还在跑的")
    for i in range(5):
        store2.create(f"q{i}")   # 这些都还没结束
    check("上限被超出（因为全部在飞）", len(store2) > 2, f"实际 {len(store2)}")
    check("仍在跑的运行没被淘汰", store2.get(running.run_id) is not None,
          "正在跑的 Run 被淘汰了，订阅方会永远挂着")

    # 它结束之后，下一个新运行到来时就会被回收
    store2.get(running.run_id).finish({"rounds": 1})
    store2.create("触发一次回收")
    check("它结束后就被回收了", store2.get(running.run_id) is None)


# ---------------------------------------------------------------------------
# 6. 多轮会话
# ---------------------------------------------------------------------------


def test_session_multiturn() -> None:
    print("\n[6] 多轮会话：同一个 session_id 共享上下文")
    c = TestClient(webapp.app)

    sid = "test_session_1"
    r1 = c.post("/api/diagnose",
                json={"question": "eth0 最近有点卡", "demo": True, "session_id": sid})
    check("第一次提问成功", r1.status_code == 200, str(r1.status_code))
    run_id = r1.json()["run_id"]

    with c.stream("GET", f"/api/diagnose/{run_id}/stream") as resp:
        _ = parse_sse(resp)

    check("会话被创建了", sid in webapp.SESSIONS, str(list(webapp.SESSIONS)))
    sess = webapp.SESSIONS[sid]
    check("会话里攒了一轮", len(sess) == 1, f"实际 {len(sess)}")
    if len(sess):
        check("存的是问题", sess.turns()[0].question == "eth0 最近有点卡",
              sess.turns()[0].question)

    r2 = c.post("/api/diagnose",
                json={"question": "那 wlan0 呢？", "demo": True, "session_id": sid})
    with c.stream("GET", f"/api/diagnose/{r2.json()['run_id']}/stream") as resp:
        _ = parse_sse(resp)
    check("会话里攒了两轮", len(webapp.SESSIONS[sid]) == 2, f"实际 {len(webapp.SESSIONS[sid])}")


# ---------------------------------------------------------------------------
# 7. 记忆接口
# ---------------------------------------------------------------------------


def test_memories_endpoint() -> None:
    print("\n[7] 记忆接口（保持只读，不要在测试里改用户数据）")
    c = TestClient(webapp.app)
    r = c.get("/api/memories")
    check("记忆接口返回 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("返回 count 字段", "count" in body, str(body)[:200])
    check("返回 memories 列表", isinstance(body.get("memories"), list), str(body)[:200])
    check("读取失败时也不崩（有 error 字段兜底）",
          r.status_code == 200 and ("error" in body or "memories" in body))


# ---------------------------------------------------------------------------


def main() -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rag"))
    from console import enable_utf8_output

    enable_utf8_output()

    print("=" * 72)
    print("Web 服务测试（全部离线：TestClient + 假 Agent，不起服务器、不花钱）")
    print("=" * 72)

    test_basic_endpoints()
    test_health_reports_real_reason()
    test_full_diagnosis_cycle()
    test_sse_resume()
    test_multiple_subscribers()
    test_thread_bridge()
    test_failure_still_terminates()
    test_runstore_eviction()
    test_session_multiturn()
    test_memories_endpoint()

    print()
    print("=" * 72)
    if _FAILED:
        print(f"[FAIL] {_FAILED} 项失败，{_PASSED} 项通过")
        return 1
    print(f"[OK] 全部 {_PASSED} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
