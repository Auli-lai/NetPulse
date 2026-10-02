"""
生成层 —— 通过百炼的 Anthropic 兼容端点调用 Qwen。

===========================================================================
Anthropic 的 Messages 协议和 OpenAI 的 Chat Completions 有三处不一样，
从 OpenAI 转过来最容易在这三行上栽跟头：
===========================================================================

  1. system 提示词是**顶层参数**，不是 messages 里的一条

     OpenAI:      messages=[{"role": "system", "content": "..."}, {...}]
     Anthropic:   system="...", messages=[{...}]

     往 messages 里塞 role="system" 会直接报错。

  2. max_tokens 是**必填**的

     OpenAI 不传有默认值，Anthropic 不传直接 400。
     这个设计有道理：它逼你想清楚这次调用最多花多少 token。

  3. 返回的 content 是**内容块列表**，不是字符串

     OpenAI:      resp.choices[0].message.content        -> str
     Anthropic:   resp.content                           -> [TextBlock, ...]

     而且块不只有文本 —— 还可能是 thinking / tool_use。
     必须判 block.type 再取 block.text，直接当字符串用会拿到一个对象。

===========================================================================
关于模型名
===========================================================================

你给的这个端点是百炼的 **Anthropic 兼容端点**，它后面跑的是 Qwen。
所以 model 要填 "qwen3-max" 这类 Qwen 的名字，**不能填 claude-opus-5**。

这个端点只实现了 /v1/messages，没有 /v1/models ——
所以任何"先列模型再选一个"的客户端逻辑在这里都会 404，
必须显式指定模型名（config.CHAT_MODEL）。
"""

from __future__ import annotations

from typing import Iterable

from config import ANTHROPIC_BASE_URL, CHAT_MODEL, MAX_TOKENS, require_api_key

# 惰性检查而不是顶层 import。
#
# 原因：eval_recall.py 只评检索质量，根本用不到生成层。
# 如果这里硬 import，没装 anthropic SDK 的人连评测都跑不起来 ——
# 检索和生成是两个独立的能力，依赖也应该分开。
try:
    import anthropic
    _IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


class LLMError(RuntimeError):
    """生成失败。上层可以据此降级成"只给检索结果、不给结论"。"""


class QwenChat:
    """Anthropic Messages 协议的客户端封装。"""

    def __init__(
        self,
        model: str = CHAT_MODEL,
        base_url: str = ANTHROPIC_BASE_URL,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        if anthropic is None:
            raise LLMError(
                f"没有安装 anthropic SDK，无法调用生成接口：{_IMPORT_ERROR}\n"
                f"    pip install anthropic\n"
                f"（只想跑检索和评测的话不需要它，"
                f"用 python eval_recall.py --offline）"
            )

        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=require_api_key(), base_url=base_url)

    # ------------------------------------------------------------------

    def chat(
        self,
        user: str,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """单轮对话。返回纯文本。"""
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.max_tokens,
                system=system or anthropic.NOT_GIVEN,  # 不传就是 NOT_GIVEN，不能传 None
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.BadRequestError as exc:
            # 400 基本都是配置问题，给可执行的排查方向，别只丢一个异常
            raise LLMError(
                f"请求被拒绝（400）：{exc.message}\n"
                f"按顺序检查：\n"
                f"  1) 模型名 '{self.model}' 在你的百炼账号里开通了吗？\n"
                f"     这个 Anthropic 兼容端点跑的是 Qwen 模型，\n"
                f"     填 claude-* 一定失败。当前可填 qwen3-max / qwen3.7-plus 等。\n"
                f"  2) QWEN_API_KEY 的类型和端点匹配吗？\n"
                f"     按量计费的 key 用 dashscope.aliyuncs.com，\n"
                f"     Coding Plan 的 key（sk-sp- 开头）要用 coding.dashscope.aliyuncs.com\n"
                f"  3) base_url 是不是多写了 /v1？\n"
                f"     要停在 {ANTHROPIC_BASE_URL}"
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError(f"API Key 无效：{exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"被限流，稍后重试：{exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(
                f"连不上 {ANTHROPIC_BASE_URL}：{exc}\n"
                f"检查网络，以及 base_url 拼写。"
            ) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"服务端错误 {exc.status_code}：{exc.message}") from exc

        return self._extract_text(resp.content)

    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(blocks: Iterable) -> str:
        """从内容块列表里取文本。

        为什么要判 type 而不是直接取 .text：
        返回的块可能是 thinking（思考）、tool_use（工具调用），
        它们没有 .text 属性，直接取会 AttributeError。
        这行判断是 Anthropic 协议和 OpenAI 协议最直观的差异。
        """
        parts: list[str] = []
        for block in blocks:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts).strip()

    def usage_note(self, prompt_tokens: int, completion_tokens: int) -> str:
        """成本估算。面试聊到 Token 成本控制时，能报出数字比空谈强。"""
        # 以下单价只是量级参考，务必以百炼控制台当期价格为准
        price_in, price_out = 2.4 / 1_000_000, 9.6 / 1_000_000
        cost = prompt_tokens * price_in + completion_tokens * price_out
        return f"约 {prompt_tokens} 输入 + {completion_tokens} 输出 tokens，≈ ¥{cost:.4f}"


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()

    try:
        # 构造也要放进 try 里 —— QwenChat() 在缺 SDK 或缺 key 时就会抛
        # LLMError，放在外面会变成一串没人看得懂的裸 traceback。
        chat = QwenChat()
        print(f"模型: {chat.model}")
        print(f"端点: {ANTHROPIC_BASE_URL}\n")
        answer = chat.chat(
            "用一句话说明 TCP 丢包率 3% 意味着什么。",
            system="你是网络运维专家，回答简洁。",
        )
        print(answer)
    except LLMError as exc:
        print(f"调用失败：\n{exc}")
