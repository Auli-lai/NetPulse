"""
LLM 客户端 —— 带 Function Calling 的 Anthropic Messages 封装。

===========================================================================
为什么本模块要自己定义一个"中立消息格式"
===========================================================================

整个项目有两条协议在跑（百炼一个 Key 三个端点）。Agent 循环不应该关心
"工具结果是放在 user 消息里还是单独一条 tool 消息"这种协议细节 ——
那是协议的活，不是编排的活。

所以循环内部统一用这套中立格式：

    {"role": "user",      "content": "网络很卡"}
    {"role": "assistant", "content": "我先看健康分", "tool_calls": [ToolCall, ...]}
    {"role": "tool",      "tool_call_id": "toolu_abc", "content": "{...}",
                          "is_error": False}

到发请求那一刻，由本模块翻译成目标协议的形状：

    to_anthropic_messages(...)  ->  Anthropic 的 content blocks

将来如果要换到 OpenAI 兼容端点（百炼那边也支持），只要再加一个
`to_openai_messages()` —— **Agent 循环一行都不用改**。

===========================================================================
Anthropic 的 tool use 有三个反直觉的点，逐个说清楚
===========================================================================

1. 【工具结果放在 user 消息里，不是一条独立的 "tool" 消息】

   OpenAI:      {"role": "tool", "tool_call_id": ..., "content": ...}
   Anthropic:   {"role": "user", "content": [{"type": "tool_result",
                                              "tool_use_id": ..., "content": ...}]}

   刚转过来的人 100% 会在这里栽一次。

2. 【同一轮的多个工具结果必须合并进**一条** user 消息】

   如果模型一次要求调 3 个工具，你不能发 3 条 user 消息。
   Anthropic 要求 assistant 的 tool_use 块后面紧跟的**那一条** user 消息里
   必须包含全部 3 个 tool_result。少一个就报 400，说工具调用没有对应结果。

   这就是下面那个 while 循环存在的原因 —— 它把连续的多条 tool 消息
   合并成一条。**这是整个翻译层最容易写错的地方。**

3. 【assistant 的内容块要原样回传】

   Anthropic 期望你把上一轮收到的 assistant content blocks 原封不动塞回去
   （包括 thinking 块）。我们收到的可能是 thinking + text + tool_use 的混合，
   自己重建成"text + tool_use"会丢掉 thinking。

   所以 Turn 上留了一个 `raw_blocks`：有就原样回传，没有才按中立格式重建。
   （百炼这个兼容端点不一定有 thinking 块，但真实 Anthropic 上是必须的 ——
   这个字段是为将来留的，成本只是一个 dict。）

===========================================================================
模型名
===========================================================================

这个端点后面跑的是 Qwen，model 要填 "qwen3-max" 这类名字，
**不能填 claude-opus-5**。CLI 的 --model 参数也遵循这条。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence

# ---------------------------------------------------------------------------
# 复用 rag/ 的端点配置，避免同一个 base_url 在两个地方各写一份
# ---------------------------------------------------------------------------
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_RAG_DIR = os.path.join(os.path.dirname(_AGENT_DIR), "rag")
if _RAG_DIR not in sys.path:
    sys.path.insert(0, _RAG_DIR)

from config import (  # type: ignore  # noqa: E402
    ANTHROPIC_BASE_URL,
    CHAT_MODEL,
    MAX_TOKENS,
    require_api_key,
)

try:
    import anthropic

    _IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


class LLMError(RuntimeError):
    """模型调用失败。Agent 循环据此决定是重试、降级还是中止。"""


# ---------------------------------------------------------------------------
# 中立数据结构
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """模型要求调用的一次工具。

    arguments 是**已经解析好的 dict**。如果模型给出的参数不是合法 JSON
    对象，解析失败的原因记在 parse_error 里，arguments 退化成 {} ——
    这样调用方可以先看 parse_error，而不是拿到一个 {} 就傻乎乎地执行。
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str | None = None


@dataclass
class Turn:
    """模型的一次回复。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    # 原始内容块，用于原样回传给 Anthropic。见模块开头第 3 点。
    raw_blocks: list[Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ChatModel(Protocol):
    """循环只依赖这个接口。测试里换成假的就能完全离线跑。"""

    def chat(self, messages: list[dict[str, Any]], tools: Sequence[dict[str, Any]]) -> Turn:
        ...


# ---------------------------------------------------------------------------
# 消息构造helper —— 保证中立格式在产生处就统一
# ---------------------------------------------------------------------------


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def assistant_message(turn: Turn) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": turn.text,
        "tool_calls": list(turn.tool_calls),
        "_raw_blocks": turn.raw_blocks,
    }


def tool_message(call: ToolCall, result: Any, is_error: bool = False) -> dict[str, Any]:
    """工具结果消息。result 会被序列化成 JSON 字符串。"""
    if isinstance(result, str):
        content = result
    else:
        content = json.dumps(result, ensure_ascii=False)
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": content,
        "is_error": is_error,
    }


# ---------------------------------------------------------------------------
# 中立格式 -> Anthropic 协议
# ---------------------------------------------------------------------------


def to_anthropic_messages(
    messages: Sequence[dict[str, Any]], preserve_raw_blocks: bool = False
) -> list[dict[str, Any]]:
    """把中立消息列表翻译成 Anthropic 的 messages 参数。

    两件必须做对的事（见模块开头）：
      · 连续的 tool 消息合并成一条 user 消息
      · assistant 的内容块正确重建（或原样回传，见下）

    关于 preserve_raw_blocks 为什么默认关：

    官方 SDK 的推荐做法是把上一轮**原样**塞回去（`content=response.content`），
    这样 thinking 块不会丢。但那个做法成立的前提是**对面就是 Anthropic**。

    百炼这里是兼容端点，它可能返回第三方 SDK 不认识的块类型，原样回传
    反而可能在序列化时炸掉 —— 而炸在发请求这一步，报错信息完全指不到这里。

    所以默认走"按 text + tool_use 重建"这条**一定安全**的路；
    真的直连 Anthropic 时再打开这个开关。
    """
    out: list[dict[str, Any]] = []
    i = 0
    n = len(messages)

    while i < n:
        msg = messages[i]
        role = msg.get("role")

        # ---- 工具结果：合并连续的若干条为一条 user 消息 ----
        if role == "tool":
            blocks: list[dict[str, Any]] = []
            while i < n and messages[i].get("role") == "tool":
                t = messages[i]
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": t.get("tool_call_id") or "",
                    "content": t.get("content", ""),
                }
                if t.get("is_error"):
                    block["is_error"] = True
                blocks.append(block)
                i += 1
            out.append({"role": "user", "content": blocks})
            continue

        # ---- assistant ----
        if role == "assistant":
            raw = msg.get("_raw_blocks") if preserve_raw_blocks else None
            if raw:
                # 原样回传，保住 thinking 块（仅直连 Anthropic 时用）
                out.append({"role": "assistant", "content": raw})
            else:
                content: list[dict[str, Any]] = []
                if msg.get("content"):
                    content.append({"type": "text", "text": msg["content"]})
                for tc in msg.get("tool_calls") or []:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                # 内容为空的 assistant 消息是非法的，跳过而不是让服务端报 400
                if not content:
                    i += 1
                    continue
                out.append({"role": "assistant", "content": content})
            i += 1
            continue

        # ---- user ----
        text = msg.get("content") or ""
        out.append({"role": "user", "content": [{"type": "text", "text": text}]})
        i += 1

    return out


def check_tool_pairing(messages: Sequence[dict[str, Any]]) -> list[str]:
    """检查"每个 tool_use 都有对应 tool_result"这条不变量，返回问题列表。

    为什么值得单独写一个函数：这是 Agent 长对话里**唯一一类会静默损坏**的
    状态。本地短对话测不出来（没触发过裁剪），跑久了才突然开始 400，
    而报错信息指向的是"消息格式错误"，看不出跟上下文管理有关。

    把它抽出来，测试里就能断言"裁剪之后配对仍然完整"，而不是等到线上才发现。
    """
    problems: list[str] = []
    pending: dict[str, str] = {}  # tool_call_id -> 工具名
    seen_results: set[str] = set()

    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                pending[tc.id] = tc.name
        elif role == "tool":
            tid = msg.get("tool_call_id")
            if tid not in pending:
                problems.append(f"第 {i} 条 tool 消息的 tool_call_id={tid!r} 没有对应的 tool_use")
            else:
                seen_results.add(tid)
        elif role == "user" and pending and seen_results != set(pending):
            # 新的一轮 user 发言出现了，但上一批工具还有没回灌结果的
            missing = set(pending) - seen_results
            problems.append(f"第 {i} 条 user 消息之前，有工具调用没有回灌结果：{sorted(missing)}")
            pending, seen_results = {}, set()

    missing = set(pending) - seen_results
    if missing:
        problems.append(f"对话结束时仍有工具调用没有回灌结果：{sorted(missing)}")
    return problems


# ---------------------------------------------------------------------------
# 真实实现
# ---------------------------------------------------------------------------


class AnthropicChatModel:
    """通过百炼的 Anthropic 兼容端点调用 Qwen，带工具调用。"""

    def __init__(
        self,
        model: str = CHAT_MODEL,
        base_url: str = ANTHROPIC_BASE_URL,
        max_tokens: int = MAX_TOKENS,
        temperature: float | None = None,
        preserve_raw_blocks: bool = False,
    ) -> None:
        if anthropic is None:
            raise LLMError(
                f"没有安装 anthropic SDK，无法调用生成接口：{_IMPORT_ERROR}\n"
                f"    pip install anthropic"
            )

        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        # 默认 False，理由见 to_anthropic_messages 的说明
        self.preserve_raw_blocks = preserve_raw_blocks
        self._client = anthropic.Anthropic(api_key=require_api_key(), base_url=base_url)
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def chat(
        self, messages: list[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> Turn:
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = list(tools)

        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=to_anthropic_messages(
                    messages, preserve_raw_blocks=self.preserve_raw_blocks
                ),
                **kwargs,
            )
        except anthropic.BadRequestError as exc:
            raise LLMError(
                f"请求被拒绝（400）：{exc.message}\n"
                f"按顺序检查：\n"
                f"  1) 模型名 '{self.model}' 在你的百炼账号里开通了吗？\n"
                f"     这个 Anthropic 兼容端点跑的是 Qwen，填 claude-* 一定失败。\n"
                f"  2) QWEN_API_KEY 的类型和端点匹配吗？\n"
                f"     按量计费的 key 用 dashscope.aliyuncs.com，\n"
                f"     Coding Plan 的 key（sk-sp- 开头）要用 coding.dashscope.aliyuncs.com\n"
                f"  3) 这个模型支持 Function Calling 吗？\n"
                f"     工具调用需要模型本身支持 tool_use，有些小模型不支持。\n"
                f"  4) base_url 是不是多写了 /v1？要停在 {ANTHROPIC_BASE_URL}"
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError(f"API Key 无效：{exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"被限流，稍后重试：{exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(
                f"连不上 {ANTHROPIC_BASE_URL}：{exc}\n检查网络，以及 base_url 拼写。"
            ) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"服务端错误 {exc.status_code}：{exc.message}") from exc

        return self._parse_response(resp)

    # ------------------------------------------------------------------

    def _parse_response(self, resp: Any) -> Turn:
        usage = getattr(resp, "usage", None)
        turn = Turn(
            stop_reason=str(getattr(resp, "stop_reason", "") or ""),
            raw_blocks=list(resp.content),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
        self.total_input_tokens += turn.input_tokens
        self.total_output_tokens += turn.output_tokens

        texts: list[str] = []
        for block in resp.content:
            btype = getattr(block, "type", None)

            if btype == "text":
                texts.append(block.text)

            elif btype == "tool_use":
                # input 正常是 dict。SDK 已经帮我们把 JSON 解好了；
                # 但模型偶尔会给出一个字符串或列表，那种情况要记下来
                # 而不是直接拿去当 kwargs 展开（会炸在 TypeError 上，
                # 且错误信息完全看不出是模型的问题）。
                raw_input = getattr(block, "input", None)
                call = ToolCall(
                    id=str(getattr(block, "id", "") or ""),
                    name=str(getattr(block, "name", "") or ""),
                )
                if isinstance(raw_input, dict):
                    call.arguments = raw_input
                    call.raw_arguments = json.dumps(raw_input, ensure_ascii=False)
                else:
                    call.arguments = {}
                    call.raw_arguments = json.dumps(raw_input, ensure_ascii=False, default=str)
                    call.parse_error = (
                        f"参数不是 JSON 对象，而是 {type(raw_input).__name__}："
                        f"{call.raw_arguments[:200]}"
                    )
                turn.tool_calls.append(call)

        turn.text = "".join(texts).strip()
        return turn

    def usage_note(self) -> str:
        """成本估算。面试聊 Token 成本控制时，能报出数字比空谈强。"""
        # 单价只是量级参考，务必以百炼控制台当期价格为准
        price_in, price_out = 2.4 / 1_000_000, 9.6 / 1_000_000
        cost = self.total_input_tokens * price_in + self.total_output_tokens * price_out
        return (
            f"累计 {self.total_input_tokens} 输入 + {self.total_output_tokens} 输出 tokens，"
            f"≈ ¥{cost:.4f}"
        )

    def usage_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
        }


# ---------------------------------------------------------------------------
# 假模型：不花钱、不需要 Key、不需要网络，就能把整条 Agent 链路跑起来
# ---------------------------------------------------------------------------


class ScriptedChatModel:
    """按剧本返回预设回复的假模型。**只用于演示和测试，不要用于真实诊断。**

    它存在的意义有两个，都很实际：

    1. **测试**。Agent 循环里最容易出错的不是"调用模型"，而是那些边界 ——
       工具报错后模型会不会自我修正、重复调用能不能被拦住、轮次上限有没有
       生效。这些用真模型根本测不稳（同一句话两次跑结果不同），
       必须用一个完全确定的假模型来测。

    2. **演示**。`python main.py --dry-run` 可以在没有 API Key、没有 D-Bus
       服务的情况下，把"模型 → 调工具 → 回灌 → 再调 → 出结论"整条链路
       和推理轨迹打出来。学习阶段看这个比看文档快得多。

    剧本是一串 Turn；每被调用一次就弹出下一个。弹完了就一直返回最后一轮
    的内容（配合 max_rounds 收尾）。
    """

    def __init__(self, script: Sequence[Turn] | None = None) -> None:
        self._script = list(script or [])
        self._index = 0
        self.calls: list[list[dict[str, Any]]] = []

    def chat(
        self, messages: list[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> Turn:
        # 存一份收到的消息，方便测试断言"循环到底发了什么给模型"
        self.calls.append([dict(m) for m in messages])
        if not self._script:
            return Turn(text="（假模型没有剧本）", stop_reason="end_turn")
        if self._index < len(self._script):
            turn = self._script[self._index]
            self._index += 1
        else:
            turn = self._script[-1]
        return turn


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()

    # 翻译层的自检：重点验证"多条工具结果合并成一条 user 消息"
    msgs = [
        user_message("网络很卡"),
        assistant_message(
            Turn(
                text="我先查一下",
                tool_calls=[
                    ToolCall(id="t1", name="get_conn_stats", arguments={}),
                    ToolCall(id="t2", name="list_interfaces", arguments={}),
                ],
            )
        ),
        tool_message(ToolCall(id="t1", name="get_conn_stats"), {"stats": {"rtt_ms": 210}}),
        tool_message(ToolCall(id="t2", name="list_interfaces"), {"interfaces": ["eth0"]}),
    ]

    rendered = to_anthropic_messages(msgs)
    print(f"中立消息 {len(msgs)} 条 -> Anthropic 消息 {len(rendered)} 条")
    print("（4 条变 3 条：两条工具结果被合并进一条 user 消息 —— 这是必须的）\n")
    for m in rendered:
        blocks = m["content"]
        kinds = [b["type"] for b in blocks] if isinstance(blocks, list) else "text"
        print(f"  role={m['role']:9s} blocks={kinds}")

    print()
    print("两条 tool_result 是否在同一条 user 消息里：", end=" ")
    last = rendered[-1]
    ok = isinstance(last["content"], list) and len(last["content"]) == 2
    print("是 ✅" if ok else "否 ❌")
    assert ok, "合并失败 —— 这会导致真实请求 400"

    print()
    print("（演示剧本已移到 demo.py：python demo.py）")
