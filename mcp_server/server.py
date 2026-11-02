"""
NetPulse 诊断工具的 MCP Server —— 把本项目的诊断能力接进任何 MCP 客户端。

===========================================================================
MCP 解决什么问题
===========================================================================

在 MCP 之前，每接一个 LLM 应用就要把工具重写一遍：

    Claude Desktop 一套        -> 各家 SDK 的 function calling 格式
    自研 Agent 一套            -> 又一套
    Cursor / VSCode 插件        -> 再来一套

N 个模型 × M 个工具 = N×M 份适配代码。这就是所谓的 **N×M 问题**。

MCP（Model Context Protocol）把它降成 N+M：工具方实现一次 Server，
模型方实现一次 Client，中间走一套标准协议（JSON-RPC 2.0）。
**它是工具接入的 USB-C。**

===========================================================================
三类原语，各自解决不同的问题（这是 MCP 最该先搞清的事）
===========================================================================

┌────────────┬──────────────────────┬──────────────────────────────────┐
│ 原语        │ 谁来控制             │ 典型用途                          │
├────────────┼──────────────────────┼──────────────────────────────────┤
│ Tools      │ **模型**决定调不调    │ 有动作、有副作用的操作            │
│ Resources  │ **应用/用户**决定加载 │ 只读的上下文数据                  │
│ Prompts    │ **用户**主动触发      │ 预设的提示词模板                  │
└────────────┴──────────────────────┴──────────────────────────────────┘

这个"谁控制"的区别不是学术分类，它直接决定了你该把某个能力做成哪一种。

本项目的取舍正好能说明问题：

  · 实时指标  -> Tool。因为"现在要不要看指标"是**模型**该判断的事，
                 它得根据用户说的话决定查不查。
  · 知识库    -> Resource。因为它是静态参考资料，**不该让模型自己决定**
                 要不要加载 —— 那是应用层或用户的事（比如用户在
                 Claude Desktop 里手动勾选一个文件）。
  · 诊断模板  -> Prompt。因为它是**用户**点一下"用这个模板诊断"才触发的。

**判断口诀：如果是模型该自己决定什么时候做的，做成 tool；
如果是人该决定给不给模型看的，做成 resource。**

===========================================================================
这个 Server 暴露了什么
===========================================================================

  Tools（6 个，从 agent/tools.py 桥接，不重写）
      get_conn_stats / list_interfaces / ping_host /
      get_network_health / get_flows / search_network_history

  Resources（6 个）
      netpulse://knowledge            知识库目录
      netpulse://knowledge/{category} 某类知识详情
      netpulse://interfaces           网卡列表（实时）
      netpulse://health               健康快照 JSON（实时）
      netpulse://logs                 日志文件列表
      netpulse://logs/{name}          某个日志文件内容

  Prompts（3 个）
      diagnose_network(symptom)       完整诊断
      explain_metric(metric, value)   解释某个指标数值
      summarize_incident(time_range)  总结某段时间发生了什么

===========================================================================
跑法
===========================================================================

    python server.py                      # stdio（给 Claude Desktop / Claude Code 用）
    python server.py --transport http     # Streamable HTTP（给远程客户端用）
    python server.py --list               # 只打印暴露了什么，不起服务

===========================================================================
⚠️ 一个必须知道的坑：stdio 模式下 stdout 是协议通道
===========================================================================

stdio 模式下，Server 和 Client 靠 **stdout** 传 JSON-RPC 帧。
**任何一条无关的 print 都会插进帧中间，把协议撕坏**，客户端报的错
还会是完全看不懂的解析错误。

好消息：MCP Python SDK **已经处理了这件事** —— 它在服务期间把
fd 1 重定向到 stderr（`mcp/server/stdio.py` 的 `_claim_fd`），
应用侧的野生 print 会自动落到 stderr 上，不会污染协议。

坏消息：它保护的是**通道**，不是**编码**。
`sys.stdout` 这个对象本身没变，在简体中文 Windows 上它的
`encoding` 仍然是 GBK —— 所以库代码里一个 emoji 的 print
照样会抛 UnicodeEncodeError，把一次工具调用搞崩。

所以本模块开头有 `_harden_streams()`：把 errors 设成 replace，
让编不出来的字符降级成 "?"。**宁可显示成问号，也不要让一次诊断失败。**
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# 路径：要 import 隔壁 agent/ 和 rag/
# ---------------------------------------------------------------------------
_MCP_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MCP_DIR)
for _sub in ("agent", "rag"):
    _p = os.path.join(_PROJECT_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mcp.server.mcpserver import MCPServer  # noqa: E402

from bridge import register_tools  # noqa: E402
from tools import ToolRunner  # noqa: E402


def _harden_streams() -> None:
    """固定控制台编码，让库代码里编不出的字符降级成 "?"，而不是抛异常。

    见模块开头：SDK 保护了**通道**，没保护**编码**。
    这里复用 rag/console.py 里那套已经验证过的逻辑 ——
    它会区分"输出到终端"和"输出到管道"，终端还会顺带把 Windows 的
    码页切到 UTF-8，比这里手写一遍 reconfigure 可靠。
    """
    try:
        from console import enable_utf8_output

        enable_utf8_output()
    except Exception:  # noqa: BLE001 —— 编码修正失败不该让服务起不来
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except (AttributeError, OSError, ValueError):
                pass


# ---------------------------------------------------------------------------
# 资源要读的东西
# ---------------------------------------------------------------------------

_SERVER_ROOT = os.path.normpath(_PROJECT_ROOT)
_LOG_DIR = os.environ.get("NETPULSE_LOG_DIR") or os.path.join(_SERVER_ROOT, "logs")


def _knowledge() -> dict[str, Any]:
    from knowledge import load_knowledge

    return load_knowledge()


def _log_files() -> list[str]:
    """列出日志目录下的文件（递归，只挑普通文件）。"""
    if not os.path.isdir(_LOG_DIR):
        return []
    found: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(_LOG_DIR):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            found.append(os.path.relpath(full, _LOG_DIR).replace(os.sep, "/"))
    return sorted(found)


def _read_log(name: str, max_bytes: int = 200_000) -> str:
    """读一个日志文件。

    ⚠️ 安全：name 来自客户端，**必须**做路径穿越检查。
    不检查的话，客户端传 "../../../etc/passwd" 就能读到任意文件。
    MCP 客户端不一定可信 —— 它可能被诱导传入恶意 URI。
    """
    # 规范化之后必须仍然落在日志目录里
    candidate = os.path.normpath(os.path.join(_LOG_DIR, name))
    if not candidate.startswith(os.path.normpath(_LOG_DIR) + os.sep):
        raise ValueError(f"非法的日志路径（越出了日志目录）：{name}")

    if not os.path.isfile(candidate):
        raise FileNotFoundError(f"没有这个日志文件：{name}")

    with open(candidate, encoding="utf-8", errors="replace") as f:
        data = f.read(max_bytes)
    if os.path.getsize(candidate) > max_bytes:
        data += f"\n\n…（文件较大，只显示前 {max_bytes} 字节）"
    return data


# ---------------------------------------------------------------------------
# 组装 server
# ---------------------------------------------------------------------------


def create_server(runner: ToolRunner | None = None, demo: bool = False) -> MCPServer:
    """造一个装好工具/资源/提示的 MCP Server。

    demo=True 时用假数据（agent/offline_demo.py 里那套），不连 D-Bus、
    不读 FAISS。用途和 `python main.py --dry-run` 一样：**先把客户端配通**，
    确认 MCP 这条链路本身是好的，再去接真实服务。

    没有它的话，第一次配 Claude Desktop 会遇到"连上了但每个工具都报错"，
    你就分不清是 MCP 配置错了还是 C++ 服务没起 —— 两个完全不同的方向。
    """
    if runner is None:
        if demo:
            from offline_demo import DemoToolRunner

            runner = DemoToolRunner()
        else:
            runner = ToolRunner()

    server = MCPServer(
        name="netpulse",
        version="1.0.0",
        instructions=(
            "NetPulse 是网络诊断工具集，基于 eBPF 在内核态采集本机网络数据。\n"
            "\n"
            "典型诊断流程：\n"
            "  1. get_network_health  —— 先拿结论和问题清单，定方向\n"
            "  2. get_conn_stats      —— 看具体指标数值，判断严重程度\n"
            "  3. get_flows           —— 定位是哪条连接造成的\n"
            "  4. ping_host           —— 主动探测，区分本地问题和目标站点问题\n"
            "  5. search_network_history —— 查历史日志和知识库\n"
            "\n"
            "注意：这些工具查的是**本机**（运行 Server 的那台机器）的网络状态。\n"
            "查历史日志和通用知识请用 search_network_history 工具，\n"
            "或者直接读 netpulse://knowledge 资源。"
        ),
    )

    register_tools(server, runner)
    _register_resources(server, runner)
    _register_prompts(server)
    return server


# ---------------------------------------------------------------------------
# 资源
# ---------------------------------------------------------------------------


def _register_resources(server: MCPServer, runner: ToolRunner) -> None:
    @server.resource(
        "netpulse://knowledge",
        name="knowledge_index",
        description="网络诊断知识库的目录：7 大类知识及各自的说明。",
        mime_type="text/markdown",
    )
    def knowledge_index() -> str:
        kb = _knowledge()
        if not kb:
            return "知识库为空或未找到（见 rag/knowledge.py 的查找逻辑）。"

        lines = ["# 网络诊断知识库\n", f"共 {len(kb)} 大类。\n"]
        for category, content in kb.items():
            desc = content.get("description", "") if isinstance(content, dict) else ""
            lines.append(f"## {category}\n{desc}\n")
            if isinstance(content, dict):
                lines.append(f"（可用字段：{', '.join(content.keys())}）\n")
        lines.append(
            "\n要读某一类的完整内容，取资源 `netpulse://knowledge/{类别名}`。"
        )
        return "\n".join(lines)

    @server.resource(
        "netpulse://knowledge/{category}",
        name="knowledge_category",
        description=(
            "某一类网络知识的完整内容（含正常范围、各级别含义、症状、排查步骤）。"
            "类别名见 netpulse://knowledge。"
        ),
        mime_type="application/json",
    )
    def knowledge_category(category: str) -> str:
        kb = _knowledge()
        if category not in kb:
            return json.dumps(
                {
                    "error": f"没有这个类别：{category}",
                    "available": sorted(kb.keys()),
                },
                ensure_ascii=False,
                indent=2,
            )
        return json.dumps(kb[category], ensure_ascii=False, indent=2)

    @server.resource(
        "netpulse://interfaces",
        name="interfaces",
        description="本机网卡列表（需要 NetPulse 服务在运行）。",
        mime_type="text/plain",
    )
    def interfaces() -> str:
        result = runner.run("list_interfaces", {})
        if "error" in result:
            # 资源读取失败时返回一段**可读的说明**，而不是抛异常。
            # 抛异常的话客户端只会看到一个笼统的错误，用户不知道为什么。
            return f"读不到网卡列表。\n\n{result['error']}"
        return "\n".join(result.get("interfaces", []))

    @server.resource(
        "netpulse://health",
        name="health_snapshot",
        description="当前网络健康快照（健康分、RTT、丢包率、信号、带宽）。",
        mime_type="application/json",
    )
    def health_snapshot() -> str:
        result = runner.run("get_conn_stats", {})
        if "error" in result:
            return json.dumps(
                {"error": "读不到健康快照", "detail": result["error"]},
                ensure_ascii=False,
                indent=2,
            )
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.resource(
        "netpulse://logs",
        name="log_index",
        description="可读的日志文件列表（由 C++ 服务运行时写入）。",
        mime_type="text/markdown",
    )
    def log_index() -> str:
        files = _log_files()
        lines = [f"日志目录：{_LOG_DIR}\n"]
        if not files:
            lines.append(
                "目录里没有日志文件。\n\n"
                "这通常意味着 C++ 服务还没运行过 —— 日志是它运行时写出来的。\n"
                "启动服务：sudo -E ./server/bin/weaknet-dbus-server\n"
            )
            return "\n".join(lines)
        lines.append(f"共 {len(files)} 个文件：\n")
        for name in files:
            lines.append(f"  · {name}")
        lines.append("\n读某个文件：资源 `netpulse://logs/{相对路径}`")
        return "\n".join(lines)

    @server.resource(
        "netpulse://logs/{name}",
        name="log_file",
        description="某个日志文件的内容（按相对路径取，见 netpulse://logs）。",
        mime_type="text/plain",
    )
    def log_file(name: str) -> str:
        try:
            return _read_log(name)
        except (ValueError, FileNotFoundError) as exc:
            return f"读不了这个日志：{exc}\n\n可用文件见 netpulse://logs"


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def _register_prompts(server: MCPServer) -> None:
    @server.prompt(
        name="diagnose_network",
        description="按标准流程诊断一个网络问题（会自动引导模型按顺序调用诊断工具）。",
    )
    def diagnose_network(symptom: str, interface: str = "") -> str:
        target = f"（重点关注网卡 {interface}）" if interface else ""
        return (
            f"我的网络出了问题：{symptom}{target}\n\n"
            "请按下面的顺序诊断，每一步都要基于工具返回的真实数据，不要凭经验猜：\n"
            "1. 先用 get_network_health 拿到整体结论和问题清单\n"
            "2. 用 get_conn_stats 看具体指标数值，判断严重程度\n"
            "3. 如果怀疑是某条连接造成的，用 get_flows 定位\n"
            "4. 用 ping_host 探测一个已知稳定的公网地址，"
            "区分『本地链路问题』和『目标站点问题』\n"
            "5. 用 search_network_history 查历史日志和知识库\n\n"
            "最后给出：根因判断 / 依据（带具体数值）/ 建议。"
            "如果数据不足以判断，明确说还缺什么。"
        )

    @server.prompt(
        name="explain_metric",
        description="解释一个网络指标的具体数值意味着什么。",
    )
    def explain_metric(metric: str, value: str) -> str:
        return (
            f"我的 {metric} 是 {value}。\n\n"
            f"请先读资源 netpulse://knowledge 找到和 {metric} 相关的类别，"
            f"再读那一类看它的正常范围和各级别定义，然后告诉我：\n"
            "1. 这个值处于什么水平（优秀/良好/一般/差）？\n"
            "2. 可能的原因是什么？\n"
            "3. 建议怎么排查？\n\n"
            "只依据知识库的内容回答，不要用你的一般网络知识补充具体数值。"
        )

    @server.prompt(
        name="summarize_incident",
        description="总结某段时间内发生了什么（查历史日志）。",
    )
    def summarize_incident(time_range: str = "最近半小时") -> str:
        return (
            f"请总结 {time_range} 内本机网络发生了什么。\n\n"
            "用 search_network_history 检索这个时间段的历史日志，"
            "按时间顺序列出发生的事件，并指出：\n"
            "· 有没有异常，异常从什么时候开始\n"
            "· 各项指标（RTT / 丢包率 / 信号强度 / 流量）的变化趋势\n"
            "· 和知识库里的哪类问题特征吻合\n\n"
            "只报告日志里确实记录了的东西。日志没记的不要推测。"
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    _harden_streams()

    parser = argparse.ArgumentParser(
        description="NetPulse 网络诊断 MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--list", action="store_true", help="列出暴露的工具/资源/提示后退出")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="用假数据跑（不连 D-Bus / 不读索引），用于先验证客户端配置是否通",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="传输方式，默认 stdio",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, default=8000, help="HTTP 监听端口")
    args = parser.parse_args()

    server = create_server(demo=args.demo)

    if args.list:
        return _list_everything(server)

    if args.transport == "stdio":
        # 这里**故意不做 stdout 检查**。
        #
        # 一开始我加了个"stdout 不是终端就告警"，后来发现那是错的：
        # stdio 模式下 stdout **本来就该是管道** —— 那正是客户端和
        # 服务端通信的方式。对它告警等于对正常配置报错，只会让人白紧张。
        # 真正需要防的"野生 print 污染协议"由 SDK 处理（见模块开头）。
        server.run(transport="stdio")
        return 0

    if args.transport == "sse":
        server.run(transport="sse", host=args.host, port=args.port)
        return 0

    server.run(transport="streamable-http", host=args.host, port=args.port)
    return 0


def _stdout_is_safe() -> bool:
    return not (hasattr(sys.stdout, "isatty") and not sys.stdout.isatty())


def _list_everything(server: MCPServer) -> int:
    """不起服务，直接把注册结果打出来。调试用。"""
    import asyncio

    async def show() -> None:
        tools = await server.list_tools()
        print(f"Tools（{len(tools)} 个）—— 模型决定调不调")
        for t in tools:
            props = list((t.input_schema or {}).get("properties", {}))
            req = (t.input_schema or {}).get("required", [])
            print(f"  · {t.name}({', '.join(props)})  required={req}")

        resources = await server.list_resources()
        templates = await server.list_resource_templates()
        print(f"\nResources（{len(resources) + len(templates)} 个）—— 应用/用户决定加载")
        for r in resources:
            print(f"  · {r.uri}")
        for t in templates:
            print(f"  · {t.uri_template}   （模板，需填占位符）")

        prompts = await server.list_prompts()
        print(f"\nPrompts（{len(prompts)} 个）—— 用户主动触发")
        for p in prompts:
            print(f"  · {p.name}  {p.description or ''}")

    asyncio.run(show())
    return 0


if __name__ == "__main__":
    sys.exit(main())
