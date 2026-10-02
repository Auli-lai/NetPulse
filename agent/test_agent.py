"""
Agent 容错测试 —— 全部离线，不需要 API Key、不需要 dbus、不需要 D-Bus 服务。

    python test_agent.py

===========================================================================
为什么这些测试必须用假模型
===========================================================================

真模型是**不确定的**：同一句话跑两次，它可能一次调 3 个工具、一次调 5 个。
所以"工具报错后模型会不会自我修正"这种事，用真模型根本没法写成断言 ——
你没法区分"代码对了"和"这次模型碰巧表现好"。

把模型换成按剧本返回的假模型（ScriptedChatModel），五种边界就都成了
确定性的、可重复的断言：

    1. 工具调用失败          -> 错误回灌，模型能看到并修正
    2. 模型重复调同一个工具   -> 检测并拦截
    3. 参数格式错误          -> 捕获并回灌，不让它变成 TypeError 崩掉
    4. 轮次上限              -> 强制收尾，而不是无限循环烧 token
    5. 上下文超长            -> 裁剪旧结果，且**不破坏 tool_use/tool_result 配对**

第 5 条尤其重要 —— 它是唯一一类会"静默损坏"的状态：本地短对话测不出来，
跑长了才突然 400。见 test_trim_keeps_pairing()。

===========================================================================
这个文件本身也是面试材料
===========================================================================

面试官问"你怎么保证 Agent 不出问题"时，把"我为循环里每一类失控都写了
一个确定性的测试，用假模型驱动"说出来，和笼统地说"我加了容错"是两个层次。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Sequence

from agent import AgentLimits
from agent import NetworkAgent as _NetworkAgent
from llm_client import (
    ScriptedChatModel,
    ToolCall,
    Turn,
    check_tool_pairing,
    to_anthropic_messages,
)
from tools import ToolRunner

# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------

_PASSED = 0
_FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  ✅ {name}")
    else:
        _FAILED += 1
        print(f"  ❌ {name}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")



def make_agent(**kwargs):
    """测试里构造 Agent 的统一入口。

    **默认关掉长期记忆** —— 测试必须是封闭的。

    这一点是踩过坑的：第一版忘了关，跑一次 test_agent.py 就往真实的
    agent/memory_store/ 里塞了 7 条假诊断。之后所有召回都被这些假数据污染，
    而且因为假诊断里有真实的网卡名和数值，看起来还挺像真的，很难发现。

    要测记忆行为的，用下面 test_long_term_memory() 里那种显式指定
    temp 目录的方式，测完自动清理。
    """
    kwargs.setdefault("use_memory", False)
    return _NetworkAgent(**kwargs)


class FakeRunner(ToolRunner):
    """不连任何外部服务的 runner。

    `results` 里没有的工具会返回一个错误 —— 这本身就有用：
    想测"工具失败"时，只要给一个不在表里的工具名就行了。
    """

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        super().__init__(client=None)
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((name, dict(arguments or {})))
        if name in self.results:
            value = self.results[name]
            # 允许用可调用对象模拟"每次结果不同"的工具
            return value() if callable(value) else value
        return {"error": f"服务不可用：{name}"}


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"c_{name}_{abs(hash(json.dumps(args, sort_keys=True))) % 10000}",
                    name=name, arguments=args)


def think(text: str, *calls: ToolCall) -> Turn:
    return Turn(text=text, tool_calls=list(calls), stop_reason="tool_use")


def final(text: str) -> Turn:
    return Turn(text=text, stop_reason="end_turn")


# ---------------------------------------------------------------------------
# 1. 正常多步链路
# ---------------------------------------------------------------------------


def test_happy_path() -> None:
    print("\n[1] 正常多步链路：模型连续调 3 个工具后收尾")
    model = ScriptedChatModel([
        think("先看健康分", call("get_network_health")),
        think("再看流量明细", call("get_conn_stats")),
        think("定位到具体连接", call("get_flows", top_n=5)),
        final("根因是 eth0 链路质量下降"),
    ])
    runner = FakeRunner({
        "get_network_health": {"quality_score": 64, "issues": ["丢包"]},
        "get_conn_stats": {"stats": {"rtt_ms": 210}},
        "get_flows": {"flow_count": 3},
    })
    result = make_agent(model=model, runner=runner).run("网络很卡")

    check("轮次 = 3", result.rounds == 3, f"实际 {result.rounds}")
    check("工具调用 = 3 次", result.tool_calls == 3, f"实际 {result.tool_calls}")
    check("正常收尾（end_turn）", result.stopped_because == "end_turn", result.stopped_because)
    check("拿到最终答案", result.answer == "根因是 eth0 链路质量下降", result.answer)
    check("轨迹里 3 个工具都记录了", len(result.steps) == 3)
    check("轨迹可渲染成文本", "第 1 轮" in result.trace())


# ---------------------------------------------------------------------------
# 2. 工具失败 -> 错误回灌
# ---------------------------------------------------------------------------


def test_tool_failure_is_fed_back() -> None:
    print("\n[2] 工具调用失败：错误必须回灌给模型，而不是让循环崩掉")
    model = ScriptedChatModel([
        think("先探一下外网", call("ping_host", hostname="8.8.8.8")),
        # 模型看到错误后换了个目标 —— 这就是"自我修正"
        think("刚才那个不通，换个 DNS 试试", call("ping_host", hostname="223.5.5.5")),
        final("外网连通性有问题"),
    ])
    runner = FakeRunner()  # 空表：所有工具都返回 error
    result = make_agent(model=model, runner=runner).run("上不了网")

    check("循环没有崩，跑完了", result.error is None, str(result.error))
    check("两次 ping 都真的执行了", len(runner.calls) == 2, str(runner.calls))
    first = result.steps[0].executions[0]
    check("第一次调用被标记为失败", first.is_error, str(first.result))

    # 关键断言：模型收到的消息里必须带 error
    second_round_messages = model.calls[1]
    tool_msgs = [m for m in second_round_messages if m.get("role") == "tool"]
    check("错误信息确实回灌进了对话", any("error" in m["content"] for m in tool_msgs),
          str([m["content"][:80] for m in tool_msgs]))
    check("回灌时 is_error 标记为 True", all(m.get("is_error") for m in tool_msgs))


# ---------------------------------------------------------------------------
# 3. 重复调用检测
# ---------------------------------------------------------------------------


def test_duplicate_call_blocked() -> None:
    print("\n[3] 重复调用检测：同一个工具+同一组参数，第 3 次起硬拦截")
    same = lambda: call("get_conn_stats")  # 每次都是完全一样的调用
    model = ScriptedChatModel([
        think("查一下", same()),
        think("再查一下", same()),
        think("还查一下", same()),
        final("就这样吧"),
    ])
    runner = FakeRunner({"get_conn_stats": {"stats": {"rtt_ms": 210}}})
    result = make_agent(model=model, runner=runner).run("网络卡")

    check("工具只真正执行了 2 次（第 3 次被拦截）",
          len(runner.calls) == 2, f"实际执行 {len(runner.calls)} 次")

    third = result.steps[2].executions[0]
    check("第 3 次被标记为干预", third.intervention is not None, str(third.intervention))
    check("拦截返回的是 duplicate_call_blocked",
          third.result.get("error") == "duplicate_call_blocked", str(third.result))
    check("拦截信息里带了解法提示", "how_to_fix" in third.result)

    # 第 2 次要执行，但要提醒模型"数据你已经有了"
    second = result.steps[1].executions[0]
    check("第 2 次执行了，但附带了提示", "_note" in second.result, str(second.result))


def test_fingerprint_ignores_key_order() -> None:
    print("\n[3b] 参数顺序不同不算新调用（指纹必须排序）")
    a = ToolCall(id="a", name="get_flows", arguments={"top_n": 5, "protocol": "TCP"})
    b = ToolCall(id="b", name="get_flows", arguments={"protocol": "TCP", "top_n": 5})
    check("两种写法指纹相同",
          _NetworkAgent._fingerprint(a) == _NetworkAgent._fingerprint(b),
          f"{_NetworkAgent._fingerprint(a)} vs {_NetworkAgent._fingerprint(b)}")

    # 反例：参数值不同必须区分开，否则会误拦
    c = ToolCall(id="c", name="get_flows", arguments={"top_n": 10, "protocol": "TCP"})
    check("参数值不同则指纹不同",
          _NetworkAgent._fingerprint(a) != _NetworkAgent._fingerprint(c))


# ---------------------------------------------------------------------------
# 4. 轮次上限
# ---------------------------------------------------------------------------


def test_max_rounds_forces_conclusion() -> None:
    print("\n[4] 轮次上限：不许无限循环，但也不许什么都不给用户")
    # 剧本里 20 轮都在调工具 —— 一个停不下来的模型。
    # 每轮参数都不一样，否则会先被重复检测拦掉，就测不到轮次上限了。
    script = [think(f"第 {i} 轮", call("get_flows", top_n=i + 1)) for i in range(20)]
    script.append(final("不该到这里"))
    model = ScriptedChatModel(script)
    runner = FakeRunner({"get_flows": {"flow_count": 1}})
    result = make_agent(model=model, runner=runner).run(
        "网络卡", limits=AgentLimits(max_rounds=3, max_tool_calls=100)
    )

    check("轮次被限制在 3 轮", result.rounds == 3, f"实际 {result.rounds}")
    check("结束原因标为 max_rounds", result.stopped_because == "max_rounds",
          result.stopped_because)
    check("仍然给出了结论（强制收尾）", bool(result.answer), repr(result.answer))
    check("工具确实只跑了 3 次", len(runner.calls) == 3, f"实际 {len(runner.calls)}")


def test_force_conclusion_sends_no_tools() -> None:
    print("\n[4b] 强制收尾必须禁用工具，否则模型会继续调")
    model = ScriptedChatModel([
        think("调一下", call("get_flows", top_n=1)),
        final("结论"),
    ])
    runner = FakeRunner({"get_flows": {"flow_count": 1}})
    agent = make_agent(
        model=model, runner=runner,
        limits=AgentLimits(max_rounds=1, max_tool_calls=99),
    )
    recorded: list[list[dict]] = []
    original = model.chat

    def spy(messages, tools):
        recorded.append(list(tools))
        return original(messages, tools)

    model.chat = spy  # type: ignore[method-assign]
    agent.run("网络卡")

    check("最后一次请求的工具列表为空", recorded and recorded[-1] == [],
          f"共 {len(recorded)} 次请求，最后一次 tools={recorded[-1] if recorded else None}")


def test_max_tool_calls() -> None:
    print("\n[4c] 单轮工具次数上限：一次要求调 10 个工具也要挡得住")
    many = [call("get_flows", top_n=i + 1) for i in range(10)]
    model = ScriptedChatModel([
        think("一口气全查了", *many),
        final("结论"),
    ])
    runner = FakeRunner({"get_flows": {"flow_count": 1}})
    result = make_agent(model=model, runner=runner).run(
        "网络卡", limits=AgentLimits(max_tool_calls=4, max_rounds=5)
    )

    check("工具调用被截断在 4 次", result.tool_calls <= 4, f"实际 {result.tool_calls}")
    check("仍然给出了结论", bool(result.answer), repr(result.answer))


# ---------------------------------------------------------------------------
# 5. 参数校验
# ---------------------------------------------------------------------------


def test_argument_validation() -> None:
    print("\n[5] 参数格式错误：捕获并回灌，不能变成 TypeError 崩掉")
    agent = make_agent(model=ScriptedChatModel([final("x")]), runner=FakeRunner())

    # 缺必填项
    problem = agent._validate_arguments(call("ping_host"))
    check("缺必填参数被拦下", problem is not None and "必填" in problem, str(problem))

    # 未定义的参数
    problem = agent._validate_arguments(ToolCall(id="x", name="ping_host",
                                                arguments={"host": "8.8.8.8"}))
    check("未定义参数被拦下", problem is not None and "未定义" in problem, str(problem))

    # 未知工具
    problem = agent._validate_arguments(ToolCall(id="x", name="nope", arguments={}))
    check("未知工具被拦下", problem is not None and "nope" in problem, str(problem))

    # 类型不对（"5" 能救就救）
    tc = ToolCall(id="x", name="get_flows", arguments={"top_n": "5"})
    problem = agent._validate_arguments(tc)
    check("字符串数字被自动纠正而不是报错", problem is None, str(problem))
    check("确实被转成了 int", tc.arguments["top_n"] == 5, repr(tc.arguments["top_n"]))

    # 真的救不了的
    tc = ToolCall(id="x", name="get_flows", arguments={"top_n": "五"})
    problem = agent._validate_arguments(tc)
    check("救不了的给明确错误", problem is not None and "整数" in problem, str(problem))

    # 合法参数必须放过
    check("合法参数通过校验",
          agent._validate_arguments(call("ping_host", hostname="8.8.8.8")) is None)


def test_parse_error_is_fed_back() -> None:
    print("\n[5b] 模型给的参数压根不是 JSON 对象时")
    bad = ToolCall(id="x", name="ping_host", arguments={},
                   raw_arguments='"8.8.8.8"',
                   parse_error="参数不是 JSON 对象，而是 str：\"8.8.8.8\"")
    model = ScriptedChatModel([
        think("我直接给个字符串", bad),
        final("我改好了"),
    ])
    runner = FakeRunner({"ping_host": {"ok": True, "rtt_ms": 20}})
    result = make_agent(model=model, runner=runner).run("ping 一下")

    check("没有执行这个非法调用", len(runner.calls) == 0, str(runner.calls))
    check("被标记为干预", result.steps[0].executions[0].intervention is not None)
    check("错误类型是 invalid_arguments",
          result.steps[0].executions[0].result.get("error") == "invalid_arguments")
    check("循环继续，最终拿到答案", result.answer == "我改好了", result.answer)


# ---------------------------------------------------------------------------
# 6. 上下文裁剪 —— 最容易静默出错的一项
# ---------------------------------------------------------------------------


def test_trim_keeps_pairing() -> None:
    print("\n[6] 上下文裁剪：可以裁内容，绝不可以删消息（否则请求 400）")
    # 造一段很长的历史：10 轮，每轮工具返回一大坨 JSON
    big = {"payload": "x" * 3000}
    script = [think(f"第 {i} 轮", call("get_flows", top_n=i + 1)) for i in range(10)]
    script.append(final("结论"))
    model = ScriptedChatModel(script)
    runner = FakeRunner({"get_flows": big})

    agent = make_agent(
        model=model, runner=runner,
        limits=AgentLimits(max_rounds=12, max_tool_calls=50, max_context_chars=8000,
                           keep_recent_tool_results=2),
    )
    result = agent.run("网络卡")

    # 取**最后一次**请求：那时 10 轮的工具结果都已经回灌完了，
    # 裁剪效果在最完整的上下文上才看得出来。
    # （取倒数第二次会少一条，因为第 N 轮的请求发出时第 N 轮的结果还没产生。）
    messages = model.calls[-1]

    problems = check_tool_pairing(messages)
    check("裁剪后 tool_use / tool_result 配对完整", not problems, "\n".join(problems))

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    trimmed = [m for m in tool_msgs if str(m.get("content", "")).startswith("[已裁剪")]
    check("确实发生了裁剪", len(trimmed) > 0,
          f"共 {len(tool_msgs)} 条工具结果，裁剪了 {len(trimmed)} 条")

    kept = [m for m in tool_msgs if not str(m.get("content", "")).startswith("[已裁剪")]
    check("保留的是最近几条（不是最早的）",
          len(kept) <= 2, f"未裁剪 {len(kept)} 条，期望 <= 2")

    check("裁剪没有减少消息条数（只换内容）",
          len(tool_msgs) == 10, f"工具消息仍有 {len(tool_msgs)} 条，期望 10")


def test_pairing_checker_catches_real_breakage() -> None:
    print("\n[6b] 校验器本身要有效（否则上面的断言就是假的）")
    from llm_client import assistant_message, tool_message, user_message

    good_msgs = [
        user_message("q"),
        assistant_message(Turn(text="t", tool_calls=[call("get_flows")])),
        tool_message(call("get_flows"), {"ok": 1}),
    ]
    check("完好的对话：无问题", not check_tool_pairing(good_msgs))

    # 故意删掉那条工具结果 —— 模拟"裁剪时错误地删了消息"
    broken = [good_msgs[0], good_msgs[1]]
    problems = check_tool_pairing(broken)
    check("缺了 tool_result 会被抓出来", len(problems) == 1, str(problems))


# ---------------------------------------------------------------------------
# 7. 模型本身挂掉时的降级
# ---------------------------------------------------------------------------


def test_llm_error_degrades_gracefully() -> None:
    print("\n[7] 模型调用失败：已采集到的数据不能丢")
    from llm_client import LLMError

    class ExplodingModel:
        def __init__(self) -> None:
            self.n = 0

        def chat(self, messages, tools):
            self.n += 1
            if self.n == 1:
                return think("先查一下", call("get_conn_stats"))
            raise LLMError("API Key 无效")

    runner = FakeRunner({"get_conn_stats": {"stats": {"rtt_ms": 210}}})
    result = make_agent(model=ExplodingModel(), runner=runner).run("网络卡")

    check("结束原因标为 llm_error", result.stopped_because == "llm_error",
          result.stopped_because)
    check("error 字段带上了原因", "API Key 无效" in (result.error or ""), str(result.error))
    check("已经拿到的工具数据保留在答案里", "rtt_ms" in result.answer, result.answer)
    check("提示了这是降级结果", "模型服务不可用" in result.answer, result.answer[:60])


# ---------------------------------------------------------------------------
# 8. 轨迹可序列化（Week 4 的 SSE 要用）
# ---------------------------------------------------------------------------


def test_trace_and_dict() -> None:
    print("\n[8] 轨迹可 JSON 序列化（Week 4 前端可视化要用）")
    model = ScriptedChatModel([
        think("查一下", call("get_flows", top_n=3)),
        final("结论"),
    ])
    runner = FakeRunner({"get_flows": {"flow_count": 1}})
    result = make_agent(model=model, runner=runner).run("网络卡")

    try:
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        ok = True
    except (TypeError, ValueError) as exc:
        payload, ok = str(exc), False

    check("to_dict() 可以 json.dumps", ok, payload[:200])
    if ok:
        parsed = json.loads(payload)
        check("结构里有 steps", "steps" in parsed)
        check("每个 step 带工具名与参数",
              parsed["steps"][0]["tools"][0]["name"] == "get_flows")
        check("带 usage 统计", "usage" in parsed)


# ---------------------------------------------------------------------------
# 9. 事件回调（SSE 的基础）
# ---------------------------------------------------------------------------


def test_event_hook() -> None:
    print("\n[9] 事件回调：推给前端的推理流")
    events: list[tuple[str, dict]] = []
    model = ScriptedChatModel([
        think("查一下", call("get_flows", top_n=3)),
        final("结论"),
    ])
    runner = FakeRunner({"get_flows": {"flow_count": 1}})
    make_agent(model=model, runner=runner,
                 on_event=lambda k, p: events.append((k, p))).run("网络卡")

    kinds = [k for k, _ in events]
    check("发出了 start 事件", "start" in kinds, str(kinds))
    check("发出了 thought 事件", "thought" in kinds)
    check("发出了 tool_result 事件", "tool_result" in kinds)
    check("发出了 finish 事件", kinds[-1] == "finish", str(kinds))

    # 最终结论必须是**独立**的 final 事件，不能混在 thought 里 ——
    # 混在一起的话终端里答案会被打印两遍（踩过）。
    check("最终结论是独立的 final 事件", "final" in kinds, str(kinds))
    final_event = next(p for k, p in events if k == "final")
    check("final 事件里带的就是最终答案", final_event["text"] == "结论",
          str(final_event.get("text")))

    tool_event = next(p for k, p in events if k == "tool_result")
    check("工具事件带耗时", "elapsed_ms" in tool_event, str(tool_event))


# ---------------------------------------------------------------------------
# 10. 真实协议渲染
# ---------------------------------------------------------------------------


def test_anthropic_rendering() -> None:
    print("\n[10] 中立消息 -> Anthropic 协议：多条工具结果必须合并成一条 user")
    from llm_client import assistant_message, tool_message, user_message

    two_calls = [call("get_conn_stats"), call("list_interfaces")]
    msgs = [
        user_message("网络卡"),
        assistant_message(Turn(text="我先看看", tool_calls=two_calls)),
        tool_message(two_calls[0], {"a": 1}),
        tool_message(two_calls[1], {"b": 2}),
    ]
    rendered = to_anthropic_messages(msgs)

    check("4 条中立消息 -> 3 条 Anthropic 消息", len(rendered) == 3, f"实际 {len(rendered)}")
    check("最后一条是 user", rendered[-1]["role"] == "user")
    blocks = rendered[-1]["content"]
    check("这条 user 里含 2 个 tool_result",
          isinstance(blocks, list) and len(blocks) == 2, str(blocks)[:200])
    check("两个 tool_result 的 id 都对得上",
          {b["tool_use_id"] for b in blocks} == {c.id for c in two_calls})

    # 错误结果要带 is_error
    err_msgs = [
        user_message("q"),
        assistant_message(Turn(text="", tool_calls=[call("ping_host")])),
        tool_message(call("ping_host"), {"error": "boom"}, is_error=True),
    ]
    block = to_anthropic_messages(err_msgs)[-1]["content"][0]
    check("失败的工具结果带 is_error=True", block.get("is_error") is True, str(block))

    # 工具定义渲染
    runner = ToolRunner()
    ath = runner.anthropic_tools()[0]
    oai = runner.openai_tools()[0]
    check("Anthropic 工具用 input_schema", "input_schema" in ath and "name" in ath)
    check("OpenAI 工具用 function.parameters",
          "parameters" in oai["function"] and oai["type"] == "function")
    check("两边的工具数量一致", len(runner.anthropic_tools()) == len(runner.openai_tools()))


# ---------------------------------------------------------------------------
# 11. 工具 schema 自检
# ---------------------------------------------------------------------------


def test_tool_schemas() -> None:
    print("\n[11] 工具 schema 自检")
    runner = ToolRunner()
    check("工具数量 >= 5", len(runner.tools) >= 5, f"实际 {len(runner.tools)}")

    for tool in runner.tools:
        check(f"{tool.name} 有 description", len(tool.description) > 50,
              f"只有 {len(tool.description)} 字")
        check(f"{tool.name} 的 parameters 是 object",
              tool.parameters.get("type") == "object")

    # 每个 required 都必须在 properties 里声明，否则模型会收到不可能满足的要求
    for tool in runner.tools:
        props = set(tool.parameters.get("properties", {}))
        required = set(tool.parameters.get("required", []))
        check(f"{tool.name} 的 required 都在 properties 里",
              required <= props, f"多出来的：{required - props}")

    # description 里应该写清"什么时候调" —— 这是工具设计的关键
    missing_when = [t.name for t in runner.tools if "什么时候调" not in t.description]
    check("所有工具的 description 都写了【什么时候调】",
          not missing_when, f"缺的：{missing_when}")


# ---------------------------------------------------------------------------
# 12. 真实检索工具接进循环（需要 rag 索引，没有就跳过）
# ---------------------------------------------------------------------------


def test_real_rag_tool_integration() -> None:
    """把**真实**的 search_network_history 接进循环跑一遍。

    前面所有测试用的都是假 runner，验证的是"循环怎么处理工具的返回"。
    这个测试反过来：循环用假模型，但工具走真实的 rag/，
    验证的是"接线对不对" —— FAISS 加载、混合检索、返回结构、
    以及最要紧的：**工具返回的东西模型真的能读懂**。

    数据链路能通、但接线接错了（字段名对不上、路径没挂上）是
    很常见的一类问题，而它在前面的测试里完全看不出来。
    """
    print("\n[12] 真实检索工具接进循环（端到端接线）")
    import os
    from tools import _RAG_DIR

    index_dir = os.path.join(_RAG_DIR, "index_store")
    if not os.path.exists(os.path.join(index_dir, "dense.faiss")):
        print(f"  ⏭️  跳过：没找到索引 {index_dir}")
        print("     （建索引：cd ../rag && python build_index.py --sample）")
        return

    # 用离线向量，这样才能在没有 API Key 的情况下跑
    os.environ.setdefault("RAG_OFFLINE", "1")

    class RealRagRunner(FakeRunner):
        """只有检索走真实实现，其余工具仍然是假的。"""

        def run(self, name, arguments=None):
            if name == "search_network_history":
                self.calls.append((name, dict(arguments or {})))
                return ToolRunner.run(self, name, arguments)
            return super().run(name, arguments)

    model = ScriptedChatModel([
        think("查历史日志", call("search_network_history",
                               query="eth0 RTT 升高 和 TCP 丢包")),
        final("根据历史日志，这是链路质量问题"),
    ])
    runner = RealRagRunner()
    result = make_agent(model=model, runner=runner).run("eth0 最近是不是有问题")

    check("没有报错", result.error is None, str(result.error))
    check("拿到了结论", bool(result.answer), repr(result.answer))

    exec0 = result.steps[0].executions[0]
    check("检索工具调用成功", not exec0.is_error, str(exec0.result)[:300])

    res = exec0.result
    check("返回里有 results 列表", isinstance(res.get("results"), list),
          str(res)[:200])
    check("检索到至少 1 条", len(res.get("results") or []) > 0,
          f"实际 {len(res.get('results') or [])} 条")

    if res.get("results"):
        first = res["results"][0]
        check("每条结果带 chunk_id", bool(first.get("chunk_id")), str(first)[:200])
        check("每条结果带 text", bool(first.get("text")), str(first)[:200])
        print(f"       召回示例：{first.get('chunk_id')} "
              f"{str(first.get('text'))[:60]}…")

    # 模型收到的工具结果必须是可读的 JSON，不是 Python repr
    msgs = model.calls[1]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    check("回灌给模型的是合法 JSON",
          all(json.loads(m["content"]) is not None for m in tool_msgs),
          str(tool_msgs)[:200])


# ---------------------------------------------------------------------------
# 13. 长期记忆
# ---------------------------------------------------------------------------


def test_long_term_memory() -> None:
    print("\n[13] 长期记忆：存、去重、召回、删、落盘、重建索引")
    import tempfile
    from memory import LongTermMemory

    with tempfile.TemporaryDirectory() as tmp:
        mem = LongTermMemory(directory=tmp, offline=True)

        check("初始为空", len(mem) == 0)
        check("空库召回不报错", mem.recall("随便问问") == [])

        r1 = mem.add(
            question="eth0 很卡",
            conclusion="## 根因判断\neth0 的 RTT 升到 210ms，判断为本地链路质量问题。",
        )
        check("记住了一条", r1 is not None and len(mem) == 1)
        check("自动抽出了网卡名", r1.interfaces == ["eth0"], str(r1.interfaces))
        check("摘要去掉了 markdown 标题", "##" not in r1.summary, r1.summary[:80])
        check("摘要里有问题原文", "eth0 很卡" in r1.summary, r1.summary[:80])

        # 同一条再记一次：不该重复存，只增加计数
        r1b = mem.add(
            question="eth0 很卡",
            conclusion="## 根因判断\neth0 的 RTT 升到 210ms，判断为本地链路质量问题。",
        )
        check("重复内容不新增记录", len(mem) == 1, f"实际 {len(mem)} 条")
        check("重复内容增加了计数", r1b.occurrences == 2, str(r1b.occurrences))

        mem.add(question="WiFi 信号弱", conclusion="wlan0 的 RSSI 是 -88dBm，信号极差。")
        check("不同的内容会新增", len(mem) == 2)

        # 召回
        hits = mem.recall("eth0 延迟高", top_k=2)
        check("能召回到东西", len(hits) > 0, f"召回 {len(hits)} 条")
        if hits:
            check("最相关的是 eth0 那条", hits[0][0].interfaces == ["eth0"],
                  str(hits[0][0].interfaces))
        mem.mark_recalled([r for r, _ in hits])
        check("召回计数被记录", any(r.recall_count > 0 for r in mem.all()))

        # 落盘 + 重新加载
        mem2 = LongTermMemory(directory=tmp, offline=True)
        check("重新加载后条数一致", len(mem2) == 2, f"实际 {len(mem2)}")

        # 索引重建（把索引删掉，应该能从 JSONL 自动重建）
        import shutil

        shutil.rmtree(os.path.join(tmp, "index"), ignore_errors=True)
        os.remove(os.path.join(tmp, "index_meta.json"))
        hits2 = mem2.recall("eth0 延迟高", top_k=2)
        check("索引丢失后能自动重建并召回", len(hits2) > 0, f"召回 {len(hits2)} 条")

        # 删
        target = mem2.all()[0].id
        check("能删掉一条", mem2.forget(target))
        check("删完条数减一", len(mem2) == 1, f"实际 {len(mem2)}")
        check("删不存在的返回 False", not mem2.forget("no_such_id"))

        # 统计
        stats = mem2.stats()
        check("统计里有条数和路径", "count" in stats and "path" in stats, str(stats))


def test_memory_survives_broken_line() -> None:
    print("\n[13b] 记忆是可手改的文件：坏行要跳过，不能让整个库打不开")
    import tempfile
    from memory import LongTermMemory

    with tempfile.TemporaryDirectory() as tmp:
        mem = LongTermMemory(directory=tmp, offline=True)
        mem.add(question="q1", conclusion="c1")
        mem.add(question="q2", conclusion="c2")

        # 模拟用户手抖改坏了一行
        path = os.path.join(tmp, "memories.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write("{这行不是合法 JSON\n")
            f.write("\n")  # 空行也要能跳过

        mem2 = LongTermMemory(directory=tmp, offline=True)
        check("坏行被跳过，好的记录还在", len(mem2) == 2, f"实际 {len(mem2)}")


def test_session_context() -> None:
    print("\n[14] 会话记忆：多轮问答要能消解指代")
    from session import Session

    sess = Session(max_turns=3)
    check("空会话渲染为空串", sess.render() == "")

    sess.add("eth0 最近有点卡", "eth0 的 RTT 升到 210ms。" * 40, interfaces=["eth0"])
    check("加了一轮", len(sess) == 1)

    text = sess.render(max_chars_per_turn=80)
    check("渲染里带上了问题", "eth0 最近有点卡" in text, text[:200])
    check("渲染里带上了网卡", "eth0" in text)
    check("长结论被截断了", len(text) < 600, f"实际 {len(text)} 字符")

    sess.add("那 wlan0 呢？", "wlan0 的 RSSI 是 -55dBm，正常。")
    check("两轮都在", len(sess) == 2)

    # 超过上限时挤掉最旧的
    sess.add("三", "c3")
    sess.add("四", "c4")
    check("轮数被限制在 max_turns", len(sess) == 3, f"实际 {len(sess)}")
    check("最旧的那轮被挤掉了", sess.turns()[0].question != "eth0 最近有点卡",
          str([t.question for t in sess.turns()]))

    # 落盘
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.json")
        sess.save(path)
        reloaded = Session.load(path)
        check("会话能落盘再读回", len(reloaded) == len(sess), f"{len(reloaded)} vs {len(sess)}")
        check("读回的最后一轮一致", reloaded.last().question == sess.last().question)


# ---------------------------------------------------------------------------
# 15. 记忆接进 Agent 循环
# ---------------------------------------------------------------------------


def test_agent_recalls_memory() -> None:
    print("\n[15] 诊断前：召回的记忆要真的进到模型看到的第一条消息里")
    import tempfile
    from memory import LongTermMemory

    with tempfile.TemporaryDirectory() as tmp:
        mem = LongTermMemory(directory=tmp, offline=True)
        mem.add(
            question="eth0 很卡",
            conclusion="## 根因判断\neth0 的 RTT 升到 210ms，是链路质量问题。",
        )

        model = ScriptedChatModel([final("这次也是链路问题")])
        agent = make_agent(model=model, runner=FakeRunner(), memory=mem, use_memory=True)
        agent.run("网络又卡了")

        first = model.calls[0][0]["content"]
        check("第一条消息里带了过往诊断", "过往类似诊断" in first, first[:250])
        check("带上了上次的结论", "210ms" in first, first[:250])
        check("标注了这是过去的判断（不是事实）", "不是事实" in first, first[:250])
        check("当前问题被放在最后", first.rstrip().endswith("网络又卡了"), first[-150:])
        check("上下文块在当前问题之前",
              first.index("过往类似诊断") < first.index("当前问题"), first[:300])


def test_agent_remembers_success() -> None:
    print("\n[16] 诊断后：成功的结论要写进长期记忆")
    import tempfile
    from memory import LongTermMemory

    with tempfile.TemporaryDirectory() as tmp:
        mem = LongTermMemory(directory=tmp, offline=True)
        model = ScriptedChatModel([
            think("查一下", call("get_conn_stats")),
            final("## 根因判断\neth0 的 RTT 偏高，是链路问题。"),
        ])
        agent = make_agent(
            model=model,
            runner=FakeRunner({"get_conn_stats": {"stats": {"rtt_ms": 210}}}),
            memory=mem,
            use_memory=True,
        )
        agent.run("网络很卡")

        check("记住了一条", len(mem) == 1, f"实际 {len(mem)} 条")
        if len(mem):
            rec = mem.all()[0]
            check("记住的是问题", rec.question == "网络很卡", rec.question)
            check("记住的是结论", "链路问题" in rec.conclusion, rec.conclusion[:80])
            check("记下了用过的工具", rec.tools_used == ["get_conn_stats"],
                  str(rec.tools_used))


def test_agent_does_not_remember_failures() -> None:
    print("\n[16b] 失败的诊断绝不能被记住（否则会污染以后的召回）")
    import tempfile
    from llm_client import LLMError
    from memory import LongTermMemory

    class ExplodingModel:
        def chat(self, messages, tools):
            raise LLMError("API Key 无效")

    with tempfile.TemporaryDirectory() as tmp:
        mem = LongTermMemory(directory=tmp, offline=True)
        agent = make_agent(
            model=ExplodingModel(), runner=FakeRunner(), memory=mem, use_memory=True
        )
        result = agent.run("网络很卡")

        check("确实是失败了", result.stopped_because == "llm_error", result.stopped_because)
        check("失败诊断没有被写进记忆", len(mem) == 0, f"实际写了 {len(mem)} 条")

    # 降级答案（有 error 字段）同理
    with tempfile.TemporaryDirectory() as tmp:
        mem = LongTermMemory(directory=tmp, offline=True)
        agent = make_agent(
            model=ScriptedChatModel([final("")]),
            runner=FakeRunner(),
            memory=mem,
            use_memory=True,
        )
        agent.run("网络很卡")
        check("空结论没有被写进记忆", len(mem) == 0, f"实际写了 {len(mem)} 条")


def test_memory_failure_does_not_break_diagnosis() -> None:
    print("\n[16c] 记忆坏了不能影响诊断 —— 它是加分项，不是必需品")

    class BrokenMemory:
        def recall(self, *a, **k):
            raise RuntimeError("记忆库损坏")

        def add(self, *a, **k):
            raise RuntimeError("记忆库损坏")

        def mark_recalled(self, *a, **k):
            raise RuntimeError("记忆库损坏")

    model = ScriptedChatModel([final("结论照样出来了")])
    agent = make_agent(
        model=model, runner=FakeRunner(), memory=BrokenMemory(), use_memory=True
    )
    result = agent.run("网络很卡")

    check("记忆炸了但诊断照常完成", result.answer == "结论照样出来了", result.answer)
    check("没有把异常记到 error 上", result.error is None, str(result.error))


def test_agent_session_multiturn() -> None:
    print("\n[17] 多轮诊断：第二句要能看懂『那 wlan0 呢』")
    from session import Session

    sess = Session()
    model = ScriptedChatModel([
        final("eth0 的 RTT 偏高，是链路问题。"),
        final("wlan0 的 RSSI 正常。"),
    ])
    agent = make_agent(model=model, runner=FakeRunner())

    agent.run("eth0 最近有点卡", session=sess)
    agent.run("那 wlan0 呢？", session=sess)

    check("会话里攒了两轮", len(sess) == 2, f"实际 {len(sess)}")
    second = model.calls[1][0]["content"]
    check("第二轮带上了第一轮的问题", "eth0 最近有点卡" in second, second[:250])
    check("第二轮带上了第一轮的结论", "链路问题" in second, second[:250])
    check("当前问题仍在最后", second.rstrip().endswith("那 wlan0 呢？"), second[-120:])

    # 不传 session 时不该有任何上下文注入
    solo = ScriptedChatModel([final("好的")])
    make_agent(model=solo, runner=FakeRunner()).run("独立问题")
    check("不传 session 时上下文干净",
          solo.calls[0][0]["content"] == "独立问题", solo.calls[0][0]["content"][:100])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    from console import enable_utf8_output

    enable_utf8_output()

    print("=" * 72)
    print("Agent 容错测试（全离线：假模型 + 假工具，不碰 API / D-Bus）")
    print("=" * 72)

    test_happy_path()
    test_tool_failure_is_fed_back()
    test_duplicate_call_blocked()
    test_fingerprint_ignores_key_order()
    test_max_rounds_forces_conclusion()
    test_force_conclusion_sends_no_tools()
    test_max_tool_calls()
    test_argument_validation()
    test_parse_error_is_fed_back()
    test_trim_keeps_pairing()
    test_pairing_checker_catches_real_breakage()
    test_llm_error_degrades_gracefully()
    test_trace_and_dict()
    test_event_hook()
    test_anthropic_rendering()
    test_tool_schemas()
    test_real_rag_tool_integration()
    test_long_term_memory()
    test_memory_survives_broken_line()
    test_session_context()
    test_agent_recalls_memory()
    test_agent_remembers_success()
    test_agent_does_not_remember_failures()
    test_memory_failure_does_not_break_diagnosis()
    test_agent_session_multiturn()

    print()
    print("=" * 72)
    if _FAILED:
        print(f"❌ {_FAILED} 项失败，{_PASSED} 项通过")
        return 1
    print(f"✅ 全部 {_PASSED} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
