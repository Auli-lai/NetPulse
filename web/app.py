"""
NetPulse Web 服务 —— 把 Agent 包成 HTTP 服务，用 SSE 实时推送推理过程。

===========================================================================
跑起来
===========================================================================

    pip install -r requirements.txt
    uvicorn app:app --reload --port 8000

然后打开 http://127.0.0.1:8000

没有 API Key、没有 C++ 服务也能看界面：

    curl -X POST http://127.0.0.1:8000/api/diagnose \
         -H 'Content-Type: application/json' \
         -d '{"question":"网络很卡","demo":true}'

`demo: true` 走假模型 + 假工具，和命令行版的 `--dry-run` 是同一套东西。

===========================================================================
接口
===========================================================================

    GET  /                          前端页面
    GET  /api/health                健康检查
    GET  /api/tools                 列出 7 个工具
    POST /api/diagnose              发起一次诊断 -> {run_id}
    GET  /api/diagnose/{id}/stream  SSE 事件流
    GET  /api/diagnose/{id}         取最终结果（轮询兜底）
    GET  /api/runs                  最近的运行列表
    GET  /api/memories              长期记忆列表

===========================================================================
为什么是"先 POST 再订阅"，而不是直接 GET 一个 SSE
===========================================================================

浏览器的 `EventSource` 只支持 GET，**不能 POST**。所以如果想让 SSE 直接带上
问题文本，只能把它塞进 URL 的查询参数：

    new EventSource('/api/stream?question=' + encodeURIComponent(q))

这样能跑，但有两个问题：
  1. 问题文本进 URL —— 会被写进访问日志、有长度限制、中文还要编码
  2. **EventSource 断线会自动重连**，而上面这个 URL 每次重连都会
     **重新发起一次诊断**。一次网络抖动就能让你连烧三次 token

所以这里用两段式：

    POST /api/diagnose      →  {run_id}        （诊断开始，事件被存下来）
    GET  /api/diagnose/{id}/stream            （订阅这个 run 的事件）

重连时浏览器会自动带上 `Last-Event-ID` 请求头，服务端从那个序号往后发，
**无缝续传，不会重跑**。SSE 协议内置了这个能力（见 runstore.py 的说明）。

===========================================================================
和其他几个目录的关系
===========================================================================

    agent/       这次服务要跑的东西
    rag/         Agent 会调它检索
    mcp_server/  另一条暴露方式（给 MCP 客户端，不是浏览器）

三者共用 `agent/tools.py` 那一份工具定义。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 路径：要 import 隔壁 agent/ 和 rag/
# ---------------------------------------------------------------------------
_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_WEB_DIR)
for _sub in ("agent", "rag"):
    _p = os.path.join(_PROJECT_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent import AgentLimits, NetworkAgent  # noqa: E402
from runstore import Run, RunStore  # noqa: E402
from session import Session  # noqa: E402
from tools import ToolRunner  # noqa: E402

STATIC_DIR = os.path.join(_WEB_DIR, "static")

app = FastAPI(
    title="NetPulse 网络诊断 Agent",
    description="eBPF 采集 + RAG 检索 + LLM 诊断，SSE 实时推送推理过程",
    version="1.0.0",
)

RUNS = RunStore(max_runs=50)
# 会话：同一个 session_id 的多次诊断共享上下文（"那 wlan0 呢"）
SESSIONS: dict[str, Session] = {}
MAX_SESSIONS = 100


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class DiagnoseRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="用自然语言描述问题")
    session_id: str | None = Field(
        None, description="会话 id。传同一个 id 的多次提问会共享上下文，能消解『那 wlan0 呢』这类指代"
    )
    demo: bool = Field(False, description="用假模型 + 假工具跑，不需要 API Key 和 C++ 服务")
    use_memory: bool = Field(True, description="是否读写长期记忆")
    max_rounds: int = Field(8, ge=1, le=20)


# ---------------------------------------------------------------------------
# 诊断的执行
# ---------------------------------------------------------------------------


def _build_agent(req: DiagnoseRequest, run: Run) -> NetworkAgent:
    """按请求造一个 Agent，把事件接到 Run 上。"""
    limits = AgentLimits(max_rounds=req.max_rounds)

    if req.demo:
        from llm_client import ScriptedChatModel
        from offline_demo import DemoToolRunner, default_demo_script

        return NetworkAgent(
            model=ScriptedChatModel(default_demo_script()),
            runner=DemoToolRunner(),
            limits=limits,
            on_event=run.push,
            use_memory=False,  # 演示不写记忆，免得假诊断污染真实记忆库
        )

    return NetworkAgent(
        model=None,  # 懒加载真实模型
        runner=ToolRunner(),
        limits=limits,
        on_event=run.push,
        use_memory=req.use_memory,
    )


def _run_agent_blocking(req: DiagnoseRequest, run: Run, session: Session | None) -> None:
    """**在工作线程里跑。** 这个函数是同步阻塞的，见 runstore.py 开头的说明。"""
    try:
        agent = _build_agent(req, run)
        result = agent.run(req.question, session=session)
        run.finish(result.to_dict())
    except Exception as exc:  # noqa: BLE001
        # 任何异常都要变成一次"正常结束"，否则订阅方的 SSE 会永远挂着
        run.fail(f"{type(exc).__name__}: {exc}")


async def _start(req: DiagnoseRequest) -> Run:
    run = RUNS.create(req.question)
    # 绑定当前事件循环，之后工作线程才能安全地往里投事件
    run.bind_loop(asyncio.get_running_loop())

    session = None
    if req.session_id:
        session = SESSIONS.get(req.session_id)
        if session is None:
            if len(SESSIONS) >= MAX_SESSIONS:
                # 简单的容量控制：扔掉最早插入的那个
                SESSIONS.pop(next(iter(SESSIONS)), None)
            session = SESSIONS[req.session_id] = Session()

    # to_thread 把阻塞函数扔进线程池 —— 事件循环不会被卡住
    asyncio.create_task(asyncio.to_thread(_run_agent_blocking, req, run, session))
    return run


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """健康检查。同时报告各个外部依赖的状态和**具体原因**。

    ⚠️ 这里踩过一个坑，值得记下来：

    第一版是这么探测 C++ 服务的：

        try:
            ToolRunner().run("list_interfaces", {})
            checks["netpulse_service"] = True
        except Exception:
            checks["netpulse_service"] = False

    **这个探测永远是"成功"的。** 因为 `ToolRunner.run()` 的设计就是
    "绝不抛异常，所有错误包成 {"error": ...} 返回"（见 agent/tools.py）——
    那是为了把错误回灌给模型让它自我修正。

    所以 try/except 抓不到任何东西，`netpulse_service` 恒为 True。
    而前端又把 "dbus_python 没装" 和 "服务没在跑" 归成同一句话，
    于是真正的原因（**没装 dbus-python**）被显示成"你的 C++ 服务没启动" ——
    用户会去反复重启一个本来就跑得好好的服务。

    修法有两处：**看返回值里有没有 error**，以及**把原始错误原样带出去**。
    顺带加一个 hints 字段，把每种失败翻译成可执行的下一步。
    """
    checks: dict[str, Any] = {}
    hints: list[str] = []

    key = os.environ.get("QWEN_API_KEY")
    if key:
        checks["api_key"] = True
        # 只露后 4 位：够你确认"是我想要的那个 key"，又不至于泄露。
        # 多个 key 混用时（比如 Coding Plan 的和按量计费的）这条特别有用。
        checks["api_key_tail"] = ("…" + key[-4:]) if len(key) > 8 else "(长度可疑)"
        if not key.startswith("sk-"):
            hints.append(
                f"QWEN_API_KEY 的值看起来不像百炼的 key（一般以 `sk-` 开头，"
                f"当前是 `{key[:6]}…`）。检查一下是不是复制错了或者少了字符。"
            )
    else:
        checks["api_key"] = False
        # 这里必须说清楚一个最常见的误解：
        # **在源码里写 os.environ.get("QWEN_API_KEY") 不等于配好了 key。**
        # 那一行的意思是"去环境变量里读"，读不到就是没有。
        # 用户会以为"我代码里写了啊"，然后反复检查代码 —— 方向完全错了。
        hints.append(
            "没读到 QWEN_API_KEY —— 模型调不动，诊断会降级成「只有数据没有结论」。\n"
            "  ⚠️ 注意：`os.environ.get(\"QWEN_API_KEY\")` 是**去环境变量里读**，"
            "不是「配好了」。代码里写了这一行，也不会凭空产生 key。\n"
            "  它必须在**启动这个服务的那个进程之前**就已经在环境里。两种做法：\n"
            "    ① 写进项目根目录的 .env（推荐，所有入口都会自动读）：\n"
            "         cp .env.example .env  然后填 QWEN_API_KEY=sk-...\n"
            "    ② 在**启动 uvicorn 的那个终端**里 export：\n"
            "         export QWEN_API_KEY='sk-...'\n"
            "  ⚠️ 两个常见陷阱：\n"
            "    · 在 A 终端 export，在 B 终端起服务 —— 环境变量不跨终端\n"
            "    · 在 PowerShell 里 export，服务跑在 WSL 里 —— 两个独立环境\n"
            "  改完之后**必须重启服务**：环境变量是进程启动时读进去的，\n"
            "  运行中的进程拿不到你在外面新设的值。\n"
            "  （只想看界面的话，打开右上角的『演示模式』，不需要 key）"
        )

    index_dir = os.path.join(_PROJECT_ROOT, "rag", "index_store")
    if os.path.exists(os.path.join(index_dir, "dense.faiss")):
        checks["rag_index"] = True
    else:
        checks["rag_index"] = False
        hints.append(
            "检索索引不存在 —— search_network_history 会失败。"
            "修法：cd rag && python build_index.py --sample"
        )

    # 报告"是哪个 Python 在跑"。
    #
    # 这一条是排查 import 失败时**最缺的信息**：
    # `sudo apt install python3-dbus` 装的是**系统 Python**（通常 /usr/bin/python3）
    # 的包。如果服务是用 venv / conda / 别的版本跑的，那个解释器**看不到**它 ——
    # 于是"明明装了却 import 不到"。
    #
    # 光报"没装 dbus-python"会把用户引向错方向：他会反复重装，
    # 而问题其实是装给了另一个 Python。所以必须把解释器路径一起报出来，
    # 用户才能自己对比。
    checks["python"] = sys.executable
    checks["python_version"] = sys.version.split()[0]

    # ---- D-Bus 与 C++ 服务：两个独立的失败点，必须分开报 ----
    try:
        import dbus  # noqa: F401

        checks["dbus_python"] = True
        checks["dbus_version"] = getattr(dbus, "__version__", "?")
    except ImportError as exc:
        checks["dbus_python"] = False
        checks["netpulse_service"] = False
        checks["netpulse_error"] = f"这个 Python 里 import 不到 dbus：{exc}"

        # ⚠️ 按平台分开说。
        #
        # 这里踩过一次：原来的提示不分平台，一律建议
        # `sudo apt install python3-dbus`。对一个在 **Windows 上**跑服务的用户，
        # 这条建议是**做不到的** —— D-Bus 是 Linux 的 IPC 机制，Windows 上没有它，
        # dbus-python 在那里根本没有可编译的底层库。
        #
        # 用户会照着提示反复尝试（sudo 不存在、/usr/bin/python3 不存在、
        # pip install 编译失败），然后以为是自己的问题。
        # **一个做不到的建议，比没有建议更糟。**
        if sys.platform == "win32":
            hints.append(
                "**你在 Windows 上跑这个服务，而 D-Bus 是 Linux 专有的** —— "
                "dbus-python 在 Windows 上**装不了**（没有 apt、没有底层库可编译）。"
                "这不是操作问题。\n"
                "  两个选择：\n"
                "    ① **在 WSL 里跑这个服务**（推荐）。WSL2 会把 localhost 转发出来，\n"
                "       Windows 的浏览器照样访问 http://localhost:8000 —— 要在 WSL 里\n"
                "       建一个 **Linux 的 venv**（Windows 的 .venv 在 WSL 里不能用）。\n"
                "    ② 就用演示模式 —— 假数据，能看到完整界面和推理流程，只是不连真实网络。"
            )
        else:
            hints.append(
                f"**当前这个 Python import 不到 dbus**"
                f"（注意：不一定是没装，可能是装给了别的 Python）。\n"
                f"  正在跑服务的解释器是：{sys.executable}（{sys.version.split()[0]}）\n"
                f"  而 `sudo apt install python3-dbus` 装的是**系统 Python**"
                f"（通常是 /usr/bin/python3）。\n"
                f"  如果你用 venv / conda / 别的版本跑服务，它看不到那个包。\n"
                f"  先验证系统 Python 有没有：\n"
                f"      /usr/bin/python3 -c \"import dbus; print('ok')\"\n"
                f"  三选一：\n"
                f"    ① 用系统 Python 跑：/usr/bin/python3 -m uvicorn app:app --port 8000\n"
                f"    ② 建 venv 时带上系统包：python3 -m venv --system-site-packages .venv\n"
                f"    ③ 装进 venv 里：pip install dbus-python"
                f"（需要先 sudo apt install libdbus-1-dev）"
            )
        return _health_payload(checks, hints)

    result = ToolRunner().run("list_interfaces", {})
    if "error" in result:
        checks["netpulse_service"] = False
        # 把原始错误带上 —— 排查靠的就是这句话，不能吞掉
        checks["netpulse_error"] = str(result["error"])[:800]
        hints.append(
            "dbus-python 装了，但连不上 NetPulse 服务。看 checks.netpulse_error 里的原文。"
            "最常见的原因是 **sudo 丢了 D-Bus 地址**："
            "服务端要用 `sudo -E ./server/bin/weaknet-dbus-server` 启动，"
            "否则它连到 root 自己的总线上，你这边看不见它。"
        )
    else:
        checks["netpulse_service"] = True
        checks["interfaces"] = result.get("interfaces")

    return _health_payload(checks, hints)


def _health_payload(checks: dict[str, Any], hints: list[str]) -> dict[str, Any]:
    return {
        "status": "ok",
        "checks": checks,
        "hints": hints,
        "runs_in_memory": len(RUNS),
        "note": (
            "checks 里为 false 的项会让对应工具降级，但不影响服务本身启动；"
            "hints 里是对应的修法。"
        ),
    }


@app.get("/api/tools")
async def list_tools() -> dict[str, Any]:
    runner = ToolRunner()
    return {
        "count": len(runner.tools),
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters.get("properties", {}),
                "required": t.parameters.get("required", []),
            }
            for t in runner.tools
        ],
    }


@app.post("/api/diagnose")
async def diagnose(req: DiagnoseRequest) -> dict[str, Any]:
    """发起一次诊断，立刻返回 run_id。事件通过 SSE 订阅。"""
    run = await _start(req)
    return {"run_id": run.run_id, "stream": f"/api/diagnose/{run.run_id}/stream"}


@app.get("/api/diagnose/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """取运行状态 / 最终结果。不订阅 SSE 时的轮询兜底。"""
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有这个运行：{run_id}")
    return run.to_dict()


@app.get("/api/diagnose/{run_id}/stream")
async def stream(run_id: str, request: Request) -> StreamingResponse:
    """SSE 事件流。

    支持断点续传：浏览器重连时会自动带上 `Last-Event-ID` 请求头，
    服务端从那个序号往后发。这是 SSE 协议内置的，不用自己实现。
    """
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有这个运行：{run_id}")

    # 优先用标准的 Last-Event-ID；也允许用 ?since= 显式指定（方便 curl 调试）
    since = 0
    header = request.headers.get("last-event-id")
    if header and header.isdigit():
        since = int(header) + 1  # Last-Event-ID 是"最后收到的那条"，所以从它 +1 开始
    elif (q := request.query_params.get("since")) and q.isdigit():
        since = int(q)

    async def event_stream() -> AsyncIterator[str]:
        # 先发一个注释帧。作用有两个：
        #   1. 让浏览器立刻认为连接已建立（有些客户端要等第一个字节）
        #   2. 绕开某些代理的响应缓冲 —— 不先吐点东西，代理会攒够一批才转发，
        #      表现就是"事件全挤在最后一起出来"，SSE 就白做了
        yield ": connected\n\n"
        async for event in run.subscribe(since=since):
            yield event.to_sse()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 一类反向代理的缓冲，否则 SSE 会被攒着一起发
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/runs")
async def recent_runs(limit: int = 20) -> dict[str, Any]:
    return {"runs": RUNS.recent(limit=min(max(limit, 1), 50))}


@app.get("/api/memories")
async def memories() -> dict[str, Any]:
    """长期记忆列表。让"它记住了什么"这件事对用户可见 —— 见 agent/memory.py。"""
    try:
        from memory import LongTermMemory

        mem = LongTermMemory()
        return {
            "count": len(mem),
            "stats": mem.stats(),
            "memories": [
                {
                    "id": r.id,
                    "date": r.created_at[:10],
                    "question": r.question,
                    "conclusion": r.conclusion[:400],
                    "interfaces": r.interfaces,
                    "occurrences": r.occurrences,
                    "recall_count": r.recall_count,
                }
                for r in mem.recent(30)
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"count": 0, "memories": [], "error": f"读取记忆失败：{exc}"}


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
