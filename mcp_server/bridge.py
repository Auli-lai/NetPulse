"""
把 agent/tools.py 里已经定义好的工具桥接成 MCP 工具 —— 不重写一遍。

===========================================================================
为什么值得单独写一个桥接层
===========================================================================

MCP 的 SDK 会**从函数的类型标注自动生成 JSON Schema**：

    @mcp.tool()
    def ping_host(hostname: str) -> dict: ...

所以最直观的写法是在 MCP Server 里把 6 个工具重新声明一遍。
**但那样工具的定义就有两份了**，而且必然分叉：

  · agent/tools.py 里的 description 改了 -> MCP 这边还是旧的
  · MCP 这边加了个参数 -> Agent 那边没有

分叉的后果特别隐蔽：**同一个模型，走 Agent 和走 MCP 表现不一样**。
你在 Agent 上测得好好的，接到 Claude Desktop 上发现它老是不调某个工具 ——
因为两边给模型看的 description 已经不是同一份了。

而 description 恰恰是这个项目里最贵的东西（它决定了模型什么时候调、
和相似工具怎么区分）。两份必然分叉，分叉必然调错。

所以这里做的是**单向桥接**：agent/tools.py 是唯一事实源，
MCP 只是它的另一种渲染 —— 和 `Tool.to_anthropic()` / `to_openai()` 是同一个思路。

===========================================================================
怎么做到的：合成函数签名
===========================================================================

MCP SDK 只接受「一个函数」并读它的 `inspect.signature()`。
而我们的 schema 是手写的 JSON Schema。两者中间的翻译靠：

    1. 按 JSON Schema 造一个 inspect.Signature
    2. 造一个真的会调 ToolRunner 的函数
    3. 把 __signature__ / __annotations__ 挂上去

于是 SDK 读到的签名和 schema 完全一致，而函数体是我们的分发器。

这有点"魔法"，所以下面写了注释解释每一步在干什么 —— 这类代码不解释清楚，
半年后自己都看不懂。
"""

from __future__ import annotations

import inspect
import os
import sys
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 挂路径：要 import 隔壁 agent/tools.py
# ---------------------------------------------------------------------------
_MCP_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MCP_DIR)
_AGENT_DIR = os.path.join(_PROJECT_ROOT, "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from tools import TOOLS, Tool, ToolRunner  # noqa: E402

# JSON Schema 的 type -> Python 类型。
# 只需要覆盖项目里实际用到的；遇到没见过的类型退回 str，
# 比抛异常好 —— 最坏情况是模型看到类型略有偏差，而不是服务起不来。
_JSON_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def build_signature(schema: dict[str, Any]) -> inspect.Signature:
    """把 JSON Schema 翻译成 inspect.Signature。

    规则：
      · required 里的参数没有默认值
      · 其余参数默认 None（MCP 会把 None 表达成 `"default": null`，
        含义就是"没提供"）

    用 KEYWORD_ONLY 是因为模型给的都是命名参数（JSON 对象按名字对），
    位置上本来就不该有歧义。
    """
    properties = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])

    params: list[inspect.Parameter] = []
    for name, spec in properties.items():
        declared = (spec or {}).get("type", "string")
        pytype = _JSON_TO_PY.get(declared, str)
        default = inspect.Parameter.empty if name in required else None
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=pytype,
            )
        )
    return inspect.Signature(params)


def make_handler(tool: Tool, runner: ToolRunner) -> Callable[..., dict[str, Any]]:
    """造一个既能被 MCP 调用、签名又和 schema 一致的分发函数。"""

    def handler(**kwargs: Any) -> dict[str, Any]:
        # 丢掉值为 None 的参数。
        #
        # 为什么要丢：可选参数在签名里默认是 None，模型不传时 MCP 也会
        # 把 None 填进来。直接透传的话，工具实现里 `interface=None`
        # 和"根本没传 interface"就没区别了 —— 恰好本项目这两者语义相同，
        # 但换个工具就不一定（比如"传 None"表示"显式清空"）。
        # 在这里统一丢掉，语义就固定成"没传"，工具实现不必各自处理。
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return runner.run(tool.name, clean)

    handler.__name__ = tool.name
    handler.__qualname__ = tool.name
    handler.__doc__ = tool.description
    handler.__signature__ = build_signature(tool.parameters)  # type: ignore[attr-defined]
    handler.__annotations__ = {
        p.name: p.annotation for p in handler.__signature__.parameters.values()  # type: ignore[attr-defined]
    }
    return handler


def register_tools(server: Any, runner: ToolRunner) -> list[str]:
    """把 agent/tools.py 里的全部工具注册到 MCP server 上。返回注册的名字列表。

    注意 description 是从 Tool 对象上取的 —— 这就是"单一事实源"的落点。
    改 agent/tools.py 里的 description，MCP 这边自动跟着变。
    """
    registered: list[str] = []
    for tool in TOOLS:
        server.add_tool(
            make_handler(tool, runner),
            name=tool.name,
            description=tool.description,
        )
        registered.append(tool.name)
    return registered


def tool_schemas() -> list[dict[str, Any]]:
    """导出 JSON Schema，给文档和测试用（不启动 server 也能看）。"""
    out = []
    for tool in TOOLS:
        out.append(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
                "required": tool.parameters.get("required", []),
                "properties": sorted(tool.parameters.get("properties", {})),
            }
        )
    return out


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "rag"))
    from console import enable_utf8_output

    enable_utf8_output()

    print(f"从 agent/tools.py 桥接 {len(TOOLS)} 个工具\n")
    for spec in tool_schemas():
        sig = build_signature(spec["input_schema"])
        print(f"  {spec['name']}{sig}")
        print(f"      description: {len(spec['description'])} 字（来自 agent/tools.py）")
        print(f"      required: {spec['required']}")
    print()
    print("用 JSON Schema 合成出的 inspect.Signature，MCP 就是读它生成协议的 schema。")
