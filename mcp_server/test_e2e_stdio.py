"""
端到端测试 —— 起一个**真的子进程**，用**真的 MCP 客户端**通过 stdio 调它。

    python test_e2e_stdio.py

===========================================================================
为什么 test_server.py 还不够
===========================================================================

test_server.py 是进程内直接调 `server.call_tool()`，它绕过了整条传输层。
能被它验证的东西里**不包含**：

  · 服务器进程能不能正常启动、能不能握手
  · JSON-RPC 帧的编解码对不对
  · stdout 有没有被野生 print 污染（stdio 模式下这是致命的）
  · 参数经过一次真正的序列化/反序列化之后还对不对
  · 客户端能不能发现工具（tools/list 返回的东西是否合法）

这些恰恰是"我配好了但就是连不上"的常见原因，而且**进程内测试永远看不出来**。

所以这个文件起一个 `python server.py --demo` 子进程，
用一个真的 ClientSession 连上去，把工具、资源、提示都走一遍。

--demo 让它用假数据，于是这个测试不依赖 C++ 服务、不依赖 API Key、
不依赖 FAISS 索引 —— **在任何机器上都能跑通**。

这也是本项目的一个模式：把外部依赖抽成接口，测试就能完全离线。

===========================================================================
顺带验证 stdio 的 stdout 保护
===========================================================================

stdio 模式下 stdout 就是协议通道，一条野生 print 就能把帧撕坏。
SDK 会在服务期间把 fd 1 重定向到 stderr（见 server.py 模块注释），
这个测试实际上也在验证那件事确实生效了 —— 因为 `--demo` 模式会加载
rag 的日志、结巴分词等一堆会 print 的模块。
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_HERE = os.path.dirname(os.path.abspath(__file__))
# console.py 在隔壁 rag/，本文件没 import server.py，所以要自己挂路径
_RAG_DIR = os.path.join(os.path.dirname(_HERE), "rag")
if _RAG_DIR not in sys.path:
    sys.path.insert(0, _RAG_DIR)

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


def _payload(result) -> dict:
    """把 call_tool 的返回解析成 dict（内容是 JSON 文本）。"""
    import json

    text = "".join(
        getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text"
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


async def run() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["server.py", "--demo"],
        cwd=_HERE,
        # 传一份干净的环境，避免外部变量干扰
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )

    print("启动子进程：python server.py --demo  （stdio 传输）\n")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # ---- 握手 ----
            init = await session.initialize()
            print(f"[1] 握手成功")
            check("拿到 serverInfo.name", bool(init.server_info.name), str(init.server_info))
            check("serverInfo.name == netpulse", init.server_info.name == "netpulse",
                  str(init.server_info))
            check("服务器声明了 tools 能力", init.capabilities.tools is not None,
                  str(init.capabilities))
            check("服务器声明了 resources 能力", init.capabilities.resources is not None,
                  str(init.capabilities))
            check("服务器声明了 prompts 能力", init.capabilities.prompts is not None,
                  str(init.capabilities))

            # ---- 工具发现 ----
            print("\n[2] 工具发现（tools/list）")
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            # 不写死数量 —— 工具集会长。判据是"agent/tools.py 里定义的全部都在"，
            # 这样以后加工具不会让这个测试变成假失败。
            import bridge as _bridge

            source_names = {t.name for t in _bridge.TOOLS}
            check("工具数量与 agent/tools.py 一致", len(tools) == len(source_names),
                  f"MCP {len(tools)} vs 源 {len(source_names)}: {sorted(names)}")
            for expected in sorted(source_names):
                check(f"  发现 {expected}", expected in names, str(sorted(names)))

            ping = next(t for t in tools if t.name == "ping_host")
            check("ping_host 的 schema 有 hostname",
                  "hostname" in (ping.input_schema.get("properties") or {}),
                  str(ping.input_schema))
            check("ping_host 的 hostname 是必填",
                  "hostname" in (ping.input_schema.get("required") or []),
                  str(ping.input_schema))
            check("description 经过传输后没丢",
                  len(ping.description or "") > 100, f"长度 {len(ping.description or '')}")

            # ---- 真正调用工具 ----
            print("\n[3] 调用工具（tools/call）")
            res = await session.call_tool("ping_host", {"hostname": "223.5.5.5"})
            check("ping_host 调用成功", not res.is_error, str(res)[:200])
            payload = _payload(res)
            check("返回是合法 JSON", "_raw" not in payload, str(payload)[:200])
            check("hostname 参数正确穿过传输层",
                  payload.get("host") == "223.5.5.5", str(payload)[:200])
            check("拿到了延迟数值", isinstance(payload.get("rtt_ms"), int),
                  str(payload)[:200])

            res = await session.call_tool("get_flows", {"top_n": 3})
            payload = _payload(res)
            check("get_flows 调用成功", not res.is_error, str(res)[:200])
            check("返回值里有连接明细",
                  "_raw" not in payload and "top_by_bps" in payload, str(payload)[:200])

            res = await session.call_tool("get_network_health", {})
            payload = _payload(res)
            check("无参数工具也能调", not res.is_error, str(res)[:200])
            check("返回健康分", "quality_score" in payload, str(payload)[:200])

            # 缺必填参数：应该是 error 结果，不是把连接搞崩
            print("\n[4] 错误路径（传错参数不能让会话崩掉）")
            try:
                res = await session.call_tool("ping_host", {})
                crashed = False
            except Exception as exc:  # noqa: BLE001
                crashed = True
                print(f"         抛异常了：{type(exc).__name__}: {exc}")
            check("缺必填参数不会弄崩会话", not crashed)

            # 会话还活着 —— 这才是关键
            res = await session.call_tool("list_interfaces", {})
            check("出错之后会话仍然可用", not res.is_error, str(res)[:200])

            # ---- 资源 ----
            print("\n[5] 资源（resources/list + resources/read）")
            resources = (await session.list_resources()).resources
            templates = (await session.list_resource_templates()).resource_templates
            uris = {str(r.uri) for r in resources}
            tmpl = {str(t.uri_template) for t in templates}
            check("发现知识库索引资源", "netpulse://knowledge" in uris, str(uris))
            check("发现健康快照资源", "netpulse://health" in uris, str(uris))
            check("发现知识库模板", "netpulse://knowledge/{category}" in tmpl, str(tmpl))
            check("发现日志模板", "netpulse://logs/{name}" in tmpl, str(tmpl))

            out = await session.read_resource("netpulse://knowledge/rtt_analysis")
            text = "".join(getattr(c, "text", "") for c in out.contents)
            check("能读到某一类知识", len(text) > 50, text[:200])
            check("知识内容里含正常范围", "normal_range" in text or "description" in text,
                  text[:250])

            out = await session.read_resource("netpulse://health")
            text = "".join(getattr(c, "text", "") for c in out.contents)
            check("能读到健康快照", "quality_score" in text or "stats" in text, text[:200])

            # ---- 提示词 ----
            print("\n[6] 提示词（prompts/list + prompts/get）")
            prompts = (await session.list_prompts()).prompts
            pnames = {p.name for p in prompts}
            check("发现 diagnose_network", "diagnose_network" in pnames, str(pnames))
            check("发现 explain_metric", "explain_metric" in pnames, str(pnames))

            got = await session.get_prompt("diagnose_network",
                                           {"symptom": "八点开始卡"})
            text = "".join(getattr(m.content, "text", "") for m in got.messages)
            check("提示词参数被正确填入", "八点开始卡" in text, text[:200])
            check("提示词内容完整", "get_network_health" in text, text[:300])

    print("\n子进程已退出。")


def main() -> int:
    from console import enable_utf8_output

    enable_utf8_output()

    print("=" * 72)
    print("MCP Server 端到端测试（真子进程 + 真 stdio 客户端）")
    print("=" * 72)
    print()

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        print(f"\n[FAIL] 端到端测试异常退出：{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 1

    print()
    print("=" * 72)
    if _FAILED:
        print(f"[FAIL] {_FAILED} 项失败，{_PASSED} 项通过")
        return 1
    print(f"[OK] 全部 {_PASSED} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
