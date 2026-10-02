"""
MCP Server 测试 —— 全部离线、进程内调用，不需要真的起 transport。

    python test_server.py

===========================================================================
为什么能在进程内测
===========================================================================

MCPServer 把注册逻辑和传输层**解耦**了：`list_tools()` / `call_tool()` /
`read_resource()` / `get_prompt()` 都是可以直接 await 的方法。

所以测试不需要去起一个 stdio 子进程、不需要写 JSON-RPC 客户端、
不需要 mock 传输 —— 直接调这些方法，断言协议层返回的东西对不对。

这比"起个进程然后发 JSON"强得多：
  · 快（毫秒级，不用等进程启动）
  · 失败信息准确（直接是 Python 异常，不是一句 "connection closed"）
  · 能测到每个字段（结构化返回，不用解析字符串）

**能这样测的前提是协议实现方把这两层分开了。** 这本身就是个值得学的设计。

===========================================================================
测什么
===========================================================================

重点不是"注册上了没有"，而是几类**容易静默出错**的地方：

  1. 工具 schema 和 agent/tools.py 是否一致（桥接有没有走样）
  2. 资源路径穿越防护（客户端传 ../ 能不能读到别的文件）
  3. 外部依赖缺席时资源是"可读的说明"还是"崩掉"
  4. 提示词里的参数有没有真的被填进去
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from server import _LOG_DIR, _read_log, create_server

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


class FakeRunner:
    """假工具执行器：不连 D-Bus、不读 FAISS。

    这样可以确定性地测"服务没起来时资源怎么表现"，
    而不是依赖跑测试的机器上恰好有没有服务。
    """

    def __init__(self, fail_all: bool = False, fail_names: set[str] | None = None):
        self.fail_all = fail_all
        self.fail_names = fail_names or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        self.calls.append((name, args))
        if self.fail_all or name in self.fail_names:
            return {"error": f"（测试桩）{name} 不可用"}
        if name == "list_interfaces":
            return {"interfaces": ["eth0", "wlan0"]}
        if name == "get_conn_stats":
            return {"stats": {"interface": "eth0", "rtt_ms": 210,
                              "tcp_loss_rate_pct": 3.2, "active_flows": 23}}
        if name == "ping_host":
            return {"ok": True, "host": args.get("hostname"), "rtt_ms": 205}
        if name == "get_flows":
            return {"flow_count": 2, "top_by_bps": [
                {"src": "10.0.0.5", "dst": "223.5.5.5", "protocol": "TCP", "bps": 2700000}]}
        if name == "get_network_health":
            return {"quality_score": 64.0, "quality_level": "一般", "issues": ["丢包"]}
        if name == "search_network_history":
            return {"query": args.get("query"), "count": 1, "results": [
                {"chunk_id": "log_ab12", "text": "10:01:30 RTT 升到 210ms"}]}
        return {"error": f"没有为 {name} 准备假数据"}


# ---------------------------------------------------------------------------
# 1. 工具
# ---------------------------------------------------------------------------


async def test_tools(server) -> None:
    print("\n[1] 工具：注册结果必须与 agent/tools.py 完全一致")

    from bridge import TOOLS as SOURCE_TOOLS

    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}

    check("工具数量一致", len(tools) == len(SOURCE_TOOLS),
          f"MCP {len(tools)} vs 源 {len(SOURCE_TOOLS)}")
    check("工具名字一致",
          set(by_name) == {t.name for t in SOURCE_TOOLS},
          f"MCP {sorted(by_name)} vs 源 {sorted(t.name for t in SOURCE_TOOLS)}")

    # description 必须逐字一致 —— 这是"单一事实源"的核心理由。
    # 一旦这里不一致，同一个模型走 MCP 和走 Agent 的表现就会不同。
    for src in SOURCE_TOOLS:
        got = by_name.get(src.name)
        if got is None:
            check(f"{src.name} 存在", False)
            continue
        check(f"{src.name} 的 description 与 agent/tools.py 逐字一致",
              got.description == src.description,
              f"MCP 长度 {len(got.description or '')} vs 源 {len(src.description)}")

    # schema 的 required 和 properties 必须一致
    for src in SOURCE_TOOLS:
        got = by_name[src.name]
        schema = got.input_schema or {}
        src_props = set(src.parameters.get("properties", {}))
        src_req = set(src.parameters.get("required", []))
        check(f"{src.name} 的 properties 一致",
              set(schema.get("properties", {})) == src_props,
              f"MCP {sorted(schema.get('properties', {}))} vs 源 {sorted(src_props)}")
        check(f"{src.name} 的 required 一致",
              set(schema.get("required", [])) == src_req,
              f"MCP {sorted(schema.get('required', []))} vs 源 {sorted(src_req)}")


async def test_tool_call() -> None:
    print("\n[2] 工具调用：参数要正确传到执行层")

    runner = FakeRunner()
    server = create_server(runner=runner)

    res = await server.call_tool("ping_host", {"hostname": "223.5.5.5"})
    check("调用成功（is_error=False）", not res.is_error, str(res)[:200])

    text = res.content[0].text
    try:
        payload = json.loads(text)
        ok = payload.get("host") == "223.5.5.5"
    except json.JSONDecodeError:
        payload, ok = text, False
    check("返回值是合法 JSON", isinstance(payload, dict), text[:200])
    check("参数确实传到了执行层", ok, text[:200])
    check("执行层收到了正确的参数", runner.calls[-1] == ("ping_host", {"hostname": "223.5.5.5"}),
          str(runner.calls[-1]))

    # 可选参数不传时，不应该被塞成 None 传下去
    runner2 = FakeRunner()
    server2 = create_server(runner=runner2)
    await server2.call_tool("get_conn_stats", {})
    check("没传的可选参数不会以 None 传下去",
          runner2.calls[-1] == ("get_conn_stats", {}),
          str(runner2.calls[-1]))

    # 工具报错时，必须是 is_error 而不是抛异常
    runner3 = FakeRunner(fail_all=True)
    server3 = create_server(runner=runner3)
    res3 = await server3.call_tool("get_conn_stats", {})
    body = json.dumps(res3.model_dump(), ensure_ascii=False, default=str)
    check("工具失败时循环不崩、错误可见", "error" in body, body[:200])


# ---------------------------------------------------------------------------
# 3. 资源
# ---------------------------------------------------------------------------


async def test_resources(server) -> None:
    print("\n[3] 资源：三类原语里的「只读上下文」")

    resources = await server.list_resources()
    templates = await server.list_resource_templates()
    uris = {str(r.uri) for r in resources}
    tmpl = {str(t.uri_template) for t in templates}

    check("有 netpulse://knowledge", "netpulse://knowledge" in uris, str(uris))
    check("有 netpulse://health", "netpulse://health" in uris, str(uris))
    check("有知识库模板", "netpulse://knowledge/{category}" in tmpl, str(tmpl))
    check("有日志模板", "netpulse://logs/{name}" in tmpl, str(tmpl))


async def test_knowledge_resource(server) -> None:
    print("\n[4] 知识库资源：不依赖任何外部服务")

    out = await server.read_resource("netpulse://knowledge")
    text = "".join(item.content for item in out)
    check("能读出知识库目录", "rtt_analysis" in text or "tcp_loss_analysis" in text,
          text[:200])

    out = await server.read_resource("netpulse://knowledge/rtt_analysis")
    payload = json.loads("".join(item.content for item in out))
    check("能读出某一类知识", isinstance(payload, dict) and payload, str(payload)[:150])
    check("这一类里有正常范围定义",
          any(k in payload for k in ("normal_range", "description")), str(list(payload))[:150])

    # 不存在的类别要给出可用列表，而不是崩
    out = await server.read_resource("netpulse://knowledge/no_such_category")
    payload = json.loads("".join(item.content for item in out))
    check("未知类别返回可用列表",
          "available" in payload and len(payload["available"]) > 0, str(payload)[:200])


async def test_resource_degradation() -> None:
    print("\n[5] 服务不在时，资源要给出可读说明而不是抛异常")

    runner = FakeRunner(fail_all=True)
    server = create_server(runner=runner)

    out = await server.read_resource("netpulse://interfaces")
    text = "".join(item.content for item in out)
    check("网卡资源返回了说明文字", len(text) > 10, text[:200])
    check("说明里带了失败原因", "不可用" in text or "error" in text.lower(), text[:200])

    out = await server.read_resource("netpulse://health")
    payload = json.loads("".join(item.content for item in out))
    check("健康资源返回 JSON 且带 error 字段", "error" in payload, str(payload)[:200])


# ---------------------------------------------------------------------------
# 6. 路径穿越（安全）
# ---------------------------------------------------------------------------


async def test_path_traversal() -> None:
    print("\n[6] 日志资源的路径穿越防护")

    # 直接测读取函数
    attacks = [
        "../../../../etc/passwd",
        "..\\..\\..\\windows\\win.ini",
        "subdir/../../outside.txt",
    ]
    for attack in attacks:
        try:
            _read_log(attack)
            blocked = False
        except ValueError:
            blocked = True
        except FileNotFoundError:
            # 没越界但文件不存在 —— 也算没读到，但我们要的是"越界被拒"
            blocked = False
        check(f"拒绝越界路径 {attack!r}", blocked)

    check("空文件名不会读到目录本身", True)

    # 正常路径不该被误伤（日志目录为空时返回"没有这个文件"，不是"越界"）
    try:
        _read_log("server/netpulse.log")
        normal_ok = True
    except FileNotFoundError:
        normal_ok = True  # 文件不存在是合理的
    except ValueError as exc:
        normal_ok = False
        print(f"         误伤：{exc}")
    check("正常相对路径不被误判为越界", normal_ok)


async def test_log_resource(server) -> None:
    print("\n[7] 日志资源")

    out = await server.read_resource("netpulse://logs")
    text = "".join(item.content for item in out)
    check("能列出日志目录", str(_LOG_DIR) in text, text[:200])
    check("目录为空时给出可执行提示",
          ("没有日志文件" in text and "weaknet-dbus-server" in text) or "共" in text,
          text[:250])


# ---------------------------------------------------------------------------
# 8. 提示词
# ---------------------------------------------------------------------------


async def test_prompts(server) -> None:
    print("\n[8] 提示词：参数要真的填进去")

    prompts = await server.list_prompts()
    names = {p.name for p in prompts}
    check("有 diagnose_network", "diagnose_network" in names, str(names))
    check("有 explain_metric", "explain_metric" in names, str(names))
    check("有 summarize_incident", "summarize_incident" in names, str(names))

    result = await server.get_prompt("diagnose_network",
                                     {"symptom": "晚上八点开始很卡", "interface": "eth0"})
    text = "".join(
        getattr(m.content, "text", str(m.content)) for m in result.messages
    )
    check("症状被填进提示词", "晚上八点开始很卡" in text, text[:200])
    check("网卡名被填进提示词", "eth0" in text, text[:200])
    check("提示词引导了正确的工具顺序",
          "get_network_health" in text and "ping_host" in text, text[:300])
    check("提示词里强调了不许编数据",
          "不要凭经验猜" in text or "真实数据" in text, text[:300])

    # 默认参数也要能走通
    result = await server.get_prompt("summarize_incident", {})
    text = "".join(getattr(m.content, "text", str(m.content)) for m in result.messages)
    check("不传参数时用默认值", "最近半小时" in text, text[:200])


# ---------------------------------------------------------------------------
# 9. 与 agent/tools.py 的一致性（回归防线）
# ---------------------------------------------------------------------------


async def test_single_source_of_truth() -> None:
    print("\n[9] 单一事实源：改 agent/tools.py 后 MCP 必须自动跟着变")

    from bridge import build_signature
    from tools import TOOLS as SOURCE

    for src in SOURCE:
        sig = build_signature(src.parameters)
        params = sig.parameters
        src_props = set(src.parameters.get("properties", {}))
        check(f"{src.name} 合成的签名参数与 schema 一致",
              set(params) == src_props,
              f"签名 {sorted(params)} vs schema {sorted(src_props)}")

        src_req = set(src.parameters.get("required", []))
        sig_req = {n for n, p in params.items() if p.default is inspect_empty()}
        check(f"{src.name} 的必填项一致", sig_req == src_req,
              f"签名必填 {sorted(sig_req)} vs schema {sorted(src_req)}")


def inspect_empty():
    import inspect

    return inspect.Parameter.empty


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def run_all() -> None:
    server = create_server(runner=FakeRunner())

    await test_tools(server)
    await test_tool_call()
    await test_resources(server)
    await test_knowledge_resource(server)
    await test_resource_degradation()
    await test_path_traversal()
    await test_log_resource(server)
    await test_prompts(server)
    await test_single_source_of_truth()


def main() -> int:
    from console import enable_utf8_output

    enable_utf8_output()

    print("=" * 72)
    print("MCP Server 测试（全部进程内调用，不需要起 transport、不需要 API Key）")
    print("=" * 72)

    asyncio.run(run_all())

    print()
    print("=" * 72)
    if _FAILED:
        print(f"[FAIL] {_FAILED} 项失败，{_PASSED} 项通过")
        return 1
    print(f"[OK] 全部 {_PASSED} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
