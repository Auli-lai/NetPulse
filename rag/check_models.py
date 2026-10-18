"""
探测你的 Key 在这个端点上**真实可用**的模型名。

    python check_models.py              # 只探模型是否存在
    python check_models.py --tools      # 顺便验证支不支持 Function Calling
    python check_models.py --model 某个名字   # 只测一个

===========================================================================
为什么需要这个脚本
===========================================================================

因为它解决的那类报错，靠读文档解决不了：

    400 InvalidParameter: Model not exist.

模型名对不对，取决于**三件事的组合**，任何一份文档都说不全：

  1. 你的账号开通了哪些模型（按量计费 / Coding Plan / Token Plan 各不相同）
  2. 那个端点接受哪些名字（不同端点支持的清单不一样）
  3. 时间 —— 模型会下线。`qwen3-max` 曾经能用，现在可能已经不在清单里了

网上和文档里能看到互相矛盾的信息（同一个名字有人说能用有人说不能），
就是因为它们在说不同的账号/端点/时间点。

**唯一可靠的办法是拿你自己的 Key 去问一次。**

===========================================================================
为什么还要单独测 Function Calling
===========================================================================

"模型存在"和"模型能调工具"是两件事。

Agent 的核心就是工具调用 —— 一个模型可能能正常聊天，但在 Anthropic 兼容
端点上不支持 tool_use（协议转换层没实现），或者返回的 tool_use 块格式不对。

所以 --tools 会发一个带 tools 的请求，看它会不会真的返回 tool_use。
**这比"模型存在"更接近你真正需要的能力。**

成本：每个模型一次 max_tokens=1 的调用，几十个 token，可以忽略。
"""

from __future__ import annotations

import argparse
import os
import sys

# ---------------------------------------------------------------------------
# 候选模型名。
# 按"这个端点最可能支持"排序 —— 一旦探到可用的就早停，省时间也省钱。
# 这份清单来自百炼文档 + 实际报错反馈，但**不代表你账号一定有**，
# 所以是"探测"不是"配置"。
# ---------------------------------------------------------------------------
CANDIDATES = [
    # 千问 Max 系列（官方文档主推）
    "qwen3.8-max",
    "qwen3.7-max",
    "qwen3.6-max-preview",
    "qwen3-max",            # ← 我们原来的默认值，很可能已下线
    "qwen-max",
    # Plus 系列
    "qwen3.7-plus",
    "qwen3.6-plus",
    "qwen3.5-plus",
    "qwen-plus",
    "qwen-plus-latest",
    # Coder 系列（工程任务强）
    "qwen3-coder-plus",
    "qwen3-coder-next",
    # 便宜快速的
    "qwen3.8-flash",
    "qwen3.7-flash",
    "qwen-flash",
    "qwen-turbo",
    # 第三方（同一端点也挂了这些）
    "deepseek-v4-pro",
    "kimi-k2.5",
    "glm-5",
]

_TOOL = [
    {
        "name": "ping_host",
        "description": "Ping a host and return latency.",
        "input_schema": {
            "type": "object",
            "properties": {"hostname": {"type": "string", "description": "target host"}},
            "required": ["hostname"],
        },
    }
]


def probe(client, model: str, with_tools: bool) -> tuple[bool, bool, str]:
    """返回 (模型可用, 工具可用, 错误信息)。"""
    try:
        kwargs = {"model": model, "max_tokens": 64,
                  "messages": [{"role": "user", "content": "Ping 8.8.8.8 for me."}]}
        if with_tools:
            kwargs["tools"] = _TOOL

        resp = client.messages.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return False, False, str(exc)[:160].replace("\n", " ")

    if not with_tools:
        return True, False, ""

    used = any(getattr(b, "type", None) == "tool_use" for b in resp.content)
    return True, used, "" if used else "模型可用，但没返回 tool_use（可能不支持工具调用）"


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from console import enable_utf8_output

    enable_utf8_output()

    import config

    try:
        import anthropic
    except ImportError:
        print("没装 anthropic SDK：pip install anthropic")
        return 1

    p = argparse.ArgumentParser(description="探测这个 Key + 端点上哪些模型可用")
    p.add_argument("--tools", action="store_true", help="顺便验证 Function Calling")
    p.add_argument("--model", help="只测指定的模型名")
    p.add_argument("--all", action="store_true", help="测完整清单，不早停")
    args = p.parse_args()

    print(f"端点：{config.ANTHROPIC_BASE_URL}")
    print(f"Key ：{'已读到' if config.API_KEY else '★ 没读到 —— 先配 .env ★'}")
    if not config.API_KEY:
        return 1
    print()

    # 注意 base_url 停在 /apps/anthropic，**不要加 /v1**（会拼成 /v1/v1/messages）
    client = anthropic.Anthropic(
        api_key=config.API_KEY, base_url=config.ANTHROPIC_BASE_URL
    )

    models = [args.model] if args.model else CANDIDATES
    ok_models: list[str] = []
    tool_models: list[str] = []

    for m in models:
        ok, tools_ok, err = probe(client, m, args.tools)
        if ok:
            tag = ""
            if args.tools:
                tag = "  ✔ 支持工具调用" if tools_ok else "  ✘ 不支持工具调用"
                if tools_ok:
                    tool_models.append(m)
            print(f"  [可用] {m}{tag}")
            ok_models.append(m)
            # 早停：找到几个够了，没必要把整个清单打完
            # （每个都是一次真实调用，虽然很便宜但没必要）
            if not args.all and not args.tools and len(ok_models) >= 3:
                print("  （已够，停止探测；要全测加 --all）")
                break
        else:
            reason = err
            if "Model not exist" in err or "model_not_exist" in err.lower():
                reason = "模型不存在 / 你的账号没开通"
            elif "Access denied" in err or "permission" in err.lower():
                reason = "无权访问（key 类型和套餐不匹配？）"
            print(f"  [不可用] {m}")
            print(f"           {reason}")

    print()
    print("=" * 70)
    if ok_models:
        print("可用的模型：")
        for m in ok_models:
            print(f"  · {m}")
        print()
        print("写进 .env：")
        print(f"  QWEN_CHAT_MODEL={ok_models[0]}")
        if args.tools:
            print(f"\n支持工具调用的：{tool_models or '★ 一个都没有 —— Agent 跑不了 ★'}")
            if not tool_models:
                print("  这个端点上的这些模型都不支持 tool_use，")
                print("  而本项目的 Agent 完全依赖工具调用。")
                print("  换一个支持 Function Calling 的模型再试。")
    else:
        print("★ 一个都没探到 ★")
        print()
        print("按顺序检查：")
        print("  1. Key 是不是百炼的？类型和端点匹配吗？")
        print("     按量计费 → dashscope.aliyuncs.com")
        print("     Coding Plan（sk-sp- 开头）→ coding.dashscope.aliyuncs.com")
        print("  2. 账号有没有欠费 / 实名认证过了吗")
        print("  3. 去百炼控制台的「模型广场」看看你账号到底有权限用哪些模型，")
        print("     把名字**原样复制**过来测：python check_models.py --model <名字>")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
