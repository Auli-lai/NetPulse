"""
ReAct Agent 循环 —— 整个项目最核心的产出物。

===========================================================================
先说结论：核心循环只有 30 行，剩下 300 行全是边界处理
===========================================================================

网上讲 ReAct 的文章都会给你这个骨架：

    while True:
        turn = model.chat(messages, tools)
        if not turn.tool_calls:
            return turn.text
        for call in turn.tool_calls:
            result = runner.run(call.name, call.arguments)
        messages.append(assistant_message(turn))
        messages.append(tool_message(call, result))

**这个骨架能跑通演示，但上不了生产。** 因为它假设了：

    · 模型给的参数永远是合法的       -> 现实中它会漏必填项、传错类型
    · 工具永远调用成功               -> 现实中服务会挂、网卡名会拼错
    · 模型不会原地打转               -> 现实中它会反复调同一个工具
    · 上下文永远不会超长             -> 现实中日志 JSON 几个回合就撑爆
    · 模型总会停下来                 -> 现实中它能转到你账户没钱

这五种情况里**任何一种**发生，上面那个骨架要么崩掉、要么悄悄烧钱。
本文件的大部分代码就是处理这五件事 —— 这也正是简历上"工具调用失败回灌、
重复调用检测、轮次上限等容错机制"那句话对应的真实工作量。

===========================================================================
一个必须知道的硬约束：裁剪上下文时不能破坏 tool_use / tool_result 配对
===========================================================================

对话历史里，assistant 的每个 tool_use 块都必须在紧接着的一条 user 消息里
找到对应的 tool_result。**少一个，请求直接 400。**

所以上下文超长时，唯一安全的做法是**把旧工具结果的内容替换成占位符**
（消息本身、配对关系、tool_call_id 全部保留），而不是删掉整条消息。

删消息是最容易犯的错 —— 本地测试时好好的（因为没触发裁剪），
线上跑长了突然开始 400，而且报错信息完全指不到裁剪逻辑。

===========================================================================
怎么测这么个东西
===========================================================================

循环里最容易错的不是"调模型"，是上面那五种边界。而真模型是**不确定的** ——
同一句话跑两次结果不同，你没法用它写断言。

所以 NetworkAgent 的 model 参数是**可注入的**，测试里换成
llm_client.ScriptedChatModel（按剧本返回的假模型），那五种边界就都能
写成确定性的断言了。见 test_agent.py。

这个"把外部依赖抽成接口以便离线测试"的做法，本身就是面试常问的点。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from llm_client import (
    AnthropicChatModel,
    ChatModel,
    LLMError,
    ToolCall,
    Turn,
    assistant_message,
    tool_message,
    user_message,
)
from tools import TOOLS, ToolRunner

# ---------------------------------------------------------------------------
# 系统提示词
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一名资深网络运维工程师，通过调用工具来诊断本机的网络问题。

【工作方式】
先观察、再推理、然后调用工具验证，而不是凭空下结论。
每次调用工具前，用一句话说明你为什么要调它。
拿到结果后判断：信息够了吗？不够就继续调；够了就给出结论。
不要在同一轮里重复调用参数完全相同的工具。

【必须遵守的规则】
1. 只依据工具返回的数据下结论。数据里没有的信息，直接说"现有数据不足以判断"，
   绝不要用你的一般网络知识编造具体数值。
2. 严格区分【观测到的现象】和【推断的原因】。工具返回的是现象，
   原因需要知识库或进一步验证来支撑。
3. 如果不同工具返回的数据相互矛盾，明确指出矛盾，不要强行圆成一个结论。
4. 工具返回的结果里如果有 "error" 字段，说明这次调用失败了，
   请判断是参数写错了、还是服务真的不可用，然后修正或换个思路。

【输出格式】
诊断结论用 Markdown，包含三部分：
  ## 根因判断 —— 直接回答用户的问题
  ## 依据 —— 列出支撑结论的具体数据（带上数值）
  ## 建议 —— 可执行的下一步操作
控制在 500 字以内，不要复述工具返回的原始 JSON。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 每次诊断开始前，从长期记忆里召回几条。
# 3 条是个平衡点：再多会把上下文撑大，而且更久远的经验反而容易误导。
RECALL_TOP_K = int(os.environ.get("NETPULSE_RECALL_TOP_K", "3"))

# 模型没给出结论时占位用的文本。
# 单独提出来是因为它**必须被识别出来** —— 它长得像个结论，
# 但其实是"什么都没有"。曾经因为它是非空字符串，被当成结论写进了长期记忆，
# 之后每次召回都会捞出一条"（模型没有给出结论）"，纯噪声。
NO_ANSWER_PLACEHOLDER = "（模型没有给出结论）"


@dataclass
class AgentLimits:
    """安全阀。每一个都对应一类真实会发生的失控。"""

    # 轮次上限：防止模型无限循环烧 token。8 轮足够走完
    # "健康分 → 连接明细 → 主动探测 → 历史关联"这条典型链路还有余量。
    max_rounds: int = 8

    # 工具调用总次数上限：模型可能在一轮里要求调 10 个工具，
    # 只限轮次挡不住这种情况。
    max_tool_calls: int = 16

    # 同一个 (工具名, 参数) 连续出现超过这个次数就硬拦截。
    # 设为 2 意味着：第 1 次正常执行，第 2 次仍然执行（状态可能变了），
    # 第 3 次直接拒绝并回灌警告。
    max_identical_calls: int = 2

    # 上下文预算（字符数，粗略 ≈ token 数 × 1.5 对中文而言）。
    # 超了就开始裁剪旧的工具结果。
    max_context_chars: int = 24_000

    # 裁剪时保留最近几条工具结果不裁剪 —— 模型当前正在用的就是这些。
    keep_recent_tool_results: int = 4


# ---------------------------------------------------------------------------
# 轨迹数据结构
# ---------------------------------------------------------------------------


@dataclass
class ToolExecution:
    """一次工具调用的完整记录。"""

    call: ToolCall
    result: dict[str, Any]
    is_error: bool = False
    elapsed_ms: float = 0.0
    # 非 None 表示这次调用被容错机制干预了（重复调用、参数非法等）
    intervention: str | None = None

    def brief(self, max_chars: int = 160) -> str:
        payload = json.dumps(self.result, ensure_ascii=False)
        if len(payload) > max_chars:
            payload = payload[:max_chars] + "…"
        return payload


@dataclass
class Step:
    """一轮推理：模型想了什么 + 调了哪些工具。"""

    round: int
    thought: str
    executions: list[ToolExecution] = field(default_factory=list)


@dataclass
class Diagnosis:
    """一次完整诊断的结果。"""

    question: str
    answer: str
    steps: list[Step] = field(default_factory=list)
    rounds: int = 0
    stopped_because: str = "end_turn"
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    # 结论是被"逼"出来的（达到轮次/工具上限后禁用工具再问一次），
    # 而不是模型自然收尾的。用户应该知道这一点 —— 逼出来的结论
    # 通常依据没那么充分。
    forced_conclusion: bool = False
    # 这条 answer 是不是**模型真的产出的内容**。
    #
    # 为什么要单独标：answer 在某些路径下会被填成占位文本或降级说明
    # （"（模型没有给出结论）"、"诊断未能完成：……"）。它们非空，
    # 所以光判 `if diagnosis.answer` 是挡不住的 —— 而把这些写进长期记忆
    # 等于往经验库里灌噪声，之后每次召回都会捞出来。
    # 用字符串匹配去挡是个坏主意（改一次文案就失效），所以显式标记。
    answer_is_model_output: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "stopped_because": self.stopped_because,
            "forced_conclusion": self.forced_conclusion,
            "answer_is_model_output": self.answer_is_model_output,
            "error": self.error,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            },
            "steps": [
                {
                    "round": s.round,
                    "thought": s.thought,
                    "tools": [
                        {
                            "name": e.call.name,
                            "arguments": e.call.arguments,
                            "is_error": e.is_error,
                            "elapsed_ms": round(e.elapsed_ms, 1),
                            "intervention": e.intervention,
                            "result": e.result,
                        }
                        for e in s.executions
                    ],
                }
                for s in self.steps
            ],
        }

    def trace(self) -> str:
        """把推理链渲染成人看的文本。这就是 Week 4 要在前端可视化的东西。"""
        lines: list[str] = []
        for step in self.steps:
            lines.append(f"── 第 {step.round} 轮 " + "─" * 46)
            if step.thought:
                lines.append(f"💭 {step.thought}")
            for e in step.executions:
                mark = "❌" if e.is_error else "🔧"
                if e.intervention:
                    mark = "⛔"
                args = json.dumps(e.call.arguments, ensure_ascii=False)
                lines.append(
                    f"  {mark} {e.call.name}({args})  [{e.elapsed_ms:.0f}ms]"
                )
                if e.intervention:
                    lines.append(f"     ⚠️  {e.intervention}")
                lines.append(f"     → {e.brief()}")
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 循环
# ---------------------------------------------------------------------------

EventHook = Callable[[str, dict[str, Any]], None]


class NetworkAgent:
    """会自己动手排查的运维 Agent。

    用法::

        agent = NetworkAgent()                  # 真实模型 + 真实工具
        result = agent.run("网络很卡，帮我看看")
        print(result.trace())
        print(result.answer)
    """

    def __init__(
        self,
        model: ChatModel | None = None,
        runner: ToolRunner | None = None,
        limits: AgentLimits | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        on_event: EventHook | None = None,
        memory: Any | None = None,
        use_memory: bool = True,
    ) -> None:
        self._model = model
        self._runner = runner or ToolRunner()
        self.limits = limits or AgentLimits()
        self.system_prompt = system_prompt
        self.on_event = on_event
        self._memory = memory
        self.use_memory = use_memory

    # ------------------------------------------------------------------

    @property
    def memory(self) -> Any:
        """懒加载长期记忆。

        懒加载在这里有两个作用：
          · 不用记忆功能的场景（离线测试、MCP 里的单次调用）完全不碰磁盘
          · 建索引可能要调 embedding API，不该在构造 Agent 时就花钱
        """
        if self._memory is None:
            from memory import LongTermMemory

            self._memory = LongTermMemory()
        # 工具层的 recall_past_diagnoses 要用**同一个实例**，
        # 否则会出现"刚诊断完，模型却检索不到"的怪现象（见 tools.bind_memory）。
        if hasattr(self._runner, "bind_memory"):
            self._runner.bind_memory(self._memory)
        return self._memory

    # ------------------------------------------------------------------

    @property
    def model(self) -> ChatModel:
        """懒加载真实模型 —— 只跑离线测试时不需要装 anthropic SDK、不需要 Key。"""
        if self._model is None:
            self._model = AnthropicChatModel()
        return self._model

    @property
    def runner(self) -> ToolRunner:
        return self._runner

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event:
            self.on_event(kind, payload)

    # ------------------------------------------------------------------

    def run(
        self,
        question: str,
        limits: AgentLimits | None = None,
        session: Any | None = None,
    ) -> Diagnosis:
        """跑一次完整诊断。**不抛异常** —— 任何失败都体现在返回值的 error 里。

        session 传入一个 Session 就能获得多轮上下文（见 session.py）：
        第二句问"那 wlan0 呢"时，模型才知道"那"指什么。
        """
        lim = limits or self.limits
        self._emit("start", question=question)

        # 记忆的读取发生在**模型第一次被调用之前** ——
        # 这样召回的内容能作为上下文的一部分一次给到，而不是事后补。
        recalled = self._recall(question)
        preamble = self._build_preamble(question, session, recalled)
        messages: list[dict[str, Any]] = [user_message(preamble)]

        steps: list[Step] = []
        # (工具名 + 参数的指纹) -> 已经调用过几次
        seen: dict[str, int] = {}
        total_calls = 0
        stopped = "end_turn"
        answer = ""
        err: str | None = None
        forced = False
        real_answer = False

        for round_no in range(1, lim.max_rounds + 1):
            self._trim_context(messages, lim)

            try:
                turn = self.model.chat(messages, self._runner.anthropic_tools())
            except LLMError as exc:
                # 模型调用失败通常是配置问题（Key 无效、模型名不对），
                # 重试没意义，直接结束并保留已有轨迹 —— 前面的工具结果本身就有价值。
                stopped = "llm_error"
                err = str(exc)
                answer = self._degraded_answer(steps, err)
                self._emit("error", message=str(exc))
                break
            except Exception as exc:  # noqa: BLE001
                stopped = "llm_error"
                err = f"{type(exc).__name__}: {exc}"
                answer = self._degraded_answer(steps, err)
                self._emit("error", message=err)
                break

            # ---- 模型不再要求调工具：这就是最终答案 ----
            #
            # 注意这里发的是 "final" 而不是 "thought"。
            # 一开始两者都发 "thought"，结果终端里答案被打印了两遍 ——
            # 一遍当"第 N 轮的思考"，一遍当结论。事件类型区分开就没这问题，
            # 而且前端也能据此知道"这条是结论，要单独渲染"。
            if not turn.wants_tools:
                if turn.text.strip():
                    answer = turn.text
                    real_answer = True
                else:
                    # 模型停下来但什么都没说。填个占位文本给用户看，
                    # 但要**明确标记它不是真结论** —— 见 answer_is_model_output 的说明。
                    answer = NO_ANSWER_PLACEHOLDER
                    real_answer = False
                stopped = turn.stop_reason or "end_turn"
                self._emit("final", round=round_no, text=answer)
                break

            self._emit("thought", round=round_no, text=turn.text)

            messages.append(assistant_message(turn))
            step = Step(round=round_no, thought=turn.text)
            steps.append(step)
            self._emit("assistant", round=round_no, tool_calls=len(turn.tool_calls))

            for call in turn.tool_calls:
                total_calls += 1
                execution = self._execute(call, seen, lim)
                step.executions.append(execution)
                messages.append(
                    tool_message(call, execution.result, is_error=execution.is_error)
                )
                self._emit(
                    "tool_result",
                    round=round_no,
                    name=call.name,
                    # arguments / raw 是给前端可视化用的。
                    # 没有它们，前端只能显示"调用了 get_flows"，
                    # 而看不出**用了什么参数、拿到了什么数据** ——
                    # 那恰恰是推理链里最有信息量的部分。
                    arguments=call.arguments,
                    raw=execution.brief(max_chars=2000),
                    is_error=execution.is_error,
                    intervention=execution.intervention,
                    elapsed_ms=execution.elapsed_ms,
                )

                if total_calls >= lim.max_tool_calls:
                    stopped = "max_tool_calls"
                    break

            if stopped == "max_tool_calls":
                # 工具次数用完了。再给模型一次机会，让它用现有信息收尾 ——
                # 直接掐掉的话用户什么结论都拿不到。
                answer, real_answer = self._force_conclusion(messages, lim)
                forced = True
                break
        else:
            stopped = "max_rounds"

        if stopped == "max_rounds":
            answer, real_answer = self._force_conclusion(messages, lim)
            forced = True

        diagnosis = Diagnosis(
            question=question,
            answer=answer,
            steps=steps,
            rounds=len(steps),
            stopped_because=stopped,
            tool_calls=total_calls,
            error=err,
            forced_conclusion=forced,
            answer_is_model_output=real_answer,
        )
        self._collect_usage(diagnosis)
        # 记忆的写入发生在**诊断成功之后** —— 失败的、降级的结论不该被记住，
        # 否则下次召回会把上次的错误当成经验。
        self._remember(question, diagnosis, session)
        self._emit("finish", stopped_because=stopped, rounds=len(steps))
        return diagnosis

    # ------------------------------------------------------------------
    # 记忆：读（诊断前）和写（诊断后）
    # ------------------------------------------------------------------

    def _recall(self, question: str) -> list[Any]:
        """召回过往类似诊断。**任何失败都只是少了个参考，不影响诊断。**"""
        if not self.use_memory:
            return []
        try:
            hits = self.memory.recall(question, top_k=RECALL_TOP_K)
        except Exception as exc:  # noqa: BLE001
            # 记忆是加分项，不是必需品。它坏了诊断照样得跑完 ——
            # 这是本模块所有记忆相关代码的统一原则。
            print(f"[warn] 长期记忆不可用，本次不做回忆（不影响诊断）：{type(exc).__name__}: {exc}")
            return []

        if hits:
            try:
                self.memory.mark_recalled([r for r, _ in hits])
            except Exception:  # noqa: BLE001
                pass
            self._emit(
                "memory_recall",
                count=len(hits),
                items=[r.one_line() for r, _ in hits],
            )
        return hits

    def _build_preamble(
        self, question: str, session: Any | None, recalled: Sequence[Any]
    ) -> str:
        """拼出第一条用户消息：会话上下文 + 过往记忆 + 当前问题。

        顺序是有讲究的：
          · 会话上下文在最前 —— 它解释"他刚才在说什么"
          · 过往记忆在中间 —— 它是参考，不是当前问题
          · **当前问题必须在最后** —— 模型对末尾最敏感，
            把问题埋在中间会让它去回答历史问题
        """
        blocks: list[str] = []

        if session is not None:
            try:
                context = session.render()
                if context:
                    blocks.append(context)
            except Exception:  # noqa: BLE001
                pass

        if recalled:
            try:
                from memory import render_recall  # 延迟导入：不用记忆就不加载

                block = render_recall(recalled)
                if block:
                    blocks.append(block)
            except Exception:  # noqa: BLE001
                pass

        if blocks:
            blocks.append(f"【当前问题】\n{question}")
            return "\n\n".join(blocks)
        return question

    def _remember(self, question: str, diagnosis: Diagnosis, session: Any | None) -> None:
        """把结论写进会话记忆和长期记忆。"""
        # 会话记忆：成功的和降级的都记（下一轮用户可能接着追问）
        if session is not None:
            try:
                session.add(question, diagnosis.answer, interfaces=None)
            except Exception:  # noqa: BLE001
                pass

        if not self.use_memory:
            return

        # 长期记忆：只有**真的得出结论**时才记。
        #
        # 三个不记的情况，每一个都有具体理由：
        #   · error           —— 模型都没调通，答案是一段错误信息
        #   · 空答案           —— 没东西可记
        #   · llm_error 降级   —— 降级答案只是把原始数据贴出来，没有"结论"
        #                         这种东西；记下来会污染以后的召回
        # 判据是"模型有没有真的产出内容"，而不是"answer 是不是非空" ——
        # 降级说明和占位文本都是非空的，光判空字符串挡不住。
        if not diagnosis.answer_is_model_output:
            return
        if diagnosis.error:
            return

        try:
            tools = [
                e.call.name
                for step in diagnosis.steps
                for e in step.executions
                if not e.is_error
            ]
            self.memory.add(
                question=question,
                conclusion=diagnosis.answer,
                tools_used=tools,
            )
            self._emit("memory_write", question=question)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 长期记忆写入失败（不影响诊断）：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # 单次工具调用：所有容错都在这一个函数里
    # ------------------------------------------------------------------

    def _execute(
        self, call: ToolCall, seen: dict[str, int], lim: AgentLimits
    ) -> ToolExecution:
        # ---- 1. 参数校验 ----
        # 校验不过就不执行，直接把原因回灌。让模型看到"你参数写错了"，
        # 比让它拿到一个空结果自己猜要有效得多。
        problem = self._validate_arguments(call)
        if problem:
            return ToolExecution(
                call=call,
                result={
                    "error": "invalid_arguments",
                    "message": problem,
                    "how_to_fix": "请按工具 schema 重新给出参数后再次调用。",
                },
                is_error=True,
                intervention=f"参数非法：{problem}",
            )

        # ---- 2. 重复调用检测 ----
        fingerprint = self._fingerprint(call)
        count = seen.get(fingerprint, 0) + 1
        seen[fingerprint] = count
        if count > lim.max_identical_calls:
            # 硬拦截。注意这里**不执行** —— 执行了结果也一样，纯浪费。
            return ToolExecution(
                call=call,
                result={
                    "error": "duplicate_call_blocked",
                    "message": (
                        f"你已经用完全相同的参数调用过 {call.name} "
                        f"{count - 1} 次了，结果不会变。"
                    ),
                    "how_to_fix": (
                        "不要再重复调用这个工具。请改用其它工具获取新信息，"
                        "或者基于已有信息直接给出结论。"
                    ),
                },
                is_error=True,
                intervention=f"重复调用被拦截（第 {count} 次）",
            )

        # ---- 3. 真正执行 ----
        start = time.perf_counter()
        result = self._runner.run(call.name, call.arguments)
        elapsed = (time.perf_counter() - start) * 1000

        is_error = "error" in result

        # ---- 4. 结果与上次一模一样时提醒一次 ----
        # 不拦截（状态确实可能没变，模型有权确认），但要明确告诉它
        # "这条信息你已经有了"，否则它会以为拿到了新数据。
        if count > 1 and not is_error:
            result = dict(result)
            result["_note"] = (
                f"这是第 {count} 次调用 {call.name}，参数与之前完全相同。"
                f"如果这些数据你已经用过了，请换一个工具或直接给结论。"
            )

        return ToolExecution(
            call=call,
            result=result,
            is_error=is_error,
            elapsed_ms=elapsed,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _fingerprint(call: ToolCall) -> str:
        """(工具名, 参数) 的指纹，用于识别重复调用。

        sort_keys 是必须的 —— 否则 {"a":1,"b":2} 和 {"b":2,"a":1}
        会被当成两次不同的调用，重复检测直接失效。
        """
        try:
            args = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            args = repr(call.arguments)
        return f"{call.name}|{args}"

    def _validate_arguments(self, call: ToolCall) -> str | None:
        """返回错误描述，None 表示通过。

        这是"参数格式错误 → 捕获并回灌"的落点。故意做得比较宽松：
        只检查**会真正导致调用失败**的问题，不做严格的类型强制 ——
        模型偶尔把 5 写成 "5" 是可以救的，没必要为它中断一轮。
        """
        if call.parse_error:
            return call.parse_error

        spec = next((t for t in TOOLS if t.name == call.name), None)
        if spec is None:
            return f"没有名为 {call.name!r} 的工具。可用工具：{[t.name for t in TOOLS]}"

        props = spec.parameters.get("properties", {})
        required = spec.parameters.get("required", [])
        args = call.arguments or {}

        # 一次把所有问题都收集齐，而不是遇到第一个就返回。
        #
        # 为什么：模型最常犯的错是**参数名拼错**（把 hostname 写成 host）。
        # 那种情况下同时存在"缺必填项"和"多未定义项"两个问题。
        # 只报第一个的话，模型补上一个 hostname 再调一次 —— 又因为 host
        # 是未定义参数失败。**白白多花一轮。** 一次说清，它一轮就能改对。
        problems: list[str] = []

        missing = [r for r in required if r not in args]
        if missing:
            problems.append(f"缺少必填参数 {missing}")

        unknown = [k for k in args if k not in props]
        if unknown:
            problems.append(
                f"出现了未定义的参数 {unknown}。这个工具接受的参数是 {sorted(props)}"
            )

        for key, value in list(args.items()):
            declared = props.get(key, {}).get("type")
            if declared == "integer" and isinstance(value, str):
                # 能救的就救一把：把 "5" 转成 5，而不是回灌错误让模型重来
                if value.lstrip("-").isdigit():
                    args[key] = int(value)
                else:
                    problems.append(f"参数 {key} 应该是整数，收到的是 {value!r}")
            elif declared == "string" and not isinstance(value, str):
                problems.append(
                    f"参数 {key} 应该是字符串，收到的是 {type(value).__name__}"
                )

        return "；".join(problems) if problems else None

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    def _estimate_chars(self, messages: Sequence[dict[str, Any]]) -> int:
        total = 0
        for m in messages:
            total += len(str(m.get("content") or ""))
            for c in m.get("tool_calls") or []:
                total += len(json.dumps(c.arguments, ensure_ascii=False)) + len(c.name)
        return total

    def _trim_context(self, messages: list[dict[str, Any]], lim: AgentLimits) -> None:
        """上下文超预算时裁剪旧的工具结果。

        ⚠️ 只替换**内容**，绝不删除消息。见模块开头关于 tool_use /
        tool_result 配对的说明 —— 删消息是这里唯一不能做的事。
        """
        size = self._estimate_chars(messages)
        if size <= lim.max_context_chars:
            return

        tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        # 保留最近 N 条，其余的把内容压成一行摘要
        protect = set(tool_indexes[-lim.keep_recent_tool_results :])

        for i in tool_indexes:
            if i in protect:
                continue
            content = messages[i].get("content") or ""
            if content.startswith("[已裁剪"):
                continue  # 已经裁过了，别反复加前缀
            name = messages[i].get("name") or "工具"
            messages[i]["content"] = (
                f"[已裁剪：{name} 的返回结果，原文 {len(content)} 字符。"
                f"如需重新获取请再次调用该工具。]"
            )
            # 裁剪后如果已经回到预算内就停下，别把能用的信息也裁掉
            if self._estimate_chars(messages) <= lim.max_context_chars:
                break

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    def _force_conclusion(
        self, messages: list[dict[str, Any]], lim: AgentLimits
    ) -> tuple[str, bool]:
        """轮次/工具次数用完时，再问模型一次，但**不给工具**。

        为什么不直接返回"达到上限"：
        用户要的是结论，不是错误码。此时上下文里已经攒了一堆真实数据，
        强迫模型做一次总结，通常能给出有依据的判断 —— 比什么都不给好得多。

        不给 tools 是关键：这样它在协议层面就无法再发起调用，
        不可能再绕一圈。
        """
        # 这里**不**往 steps 里加一条假轮次。
        #
        # 加的话 Diagnosis.rounds 会把"被要求收尾"也算成一轮推理，
        # 于是 max_rounds=3 却报 rounds=4 —— 数字对不上，排查时会被误导。
        # 收尾这件事本身用 Diagnosis.forced_conclusion 标记就够了。
        self._emit("forcing_conclusion")
        nudge = user_message(
            "已达到本轮诊断的工具调用上限。请**不要再调用任何工具**，"
            "直接基于上面已经获得的数据给出结论。\n"
            "如果现有数据不足以判断根因，就明确说明还缺什么信息。"
        )
        messages.append(nudge)

        try:
            turn = self.model.chat(messages, [])  # 空 tools 列表 = 不允许调用
        except LLMError as exc:
            # 返回 (文本, 是不是真结论)。false 表示这段文字是错误说明，
            # 不能当结论用 —— 尤其不能被写进长期记忆。
            return f"（达到工具调用上限，且生成结论时失败：{exc}）", False
        except Exception as exc:  # noqa: BLE001
            return f"（达到工具调用上限，且生成结论时失败：{type(exc).__name__}: {exc}）", False

        if turn.text.strip():
            return turn.text, True
        return "（达到工具调用上限，模型未给出结论）", False

    def _degraded_answer(self, steps: list[Step], err: str) -> str:
        """模型挂了，但工具可能已经调成功过 —— 那些结果是真实数据，不该丢。

        降级成"只给数据不給结论"，和 rag 那边生成失败时的处理是同一个思路：
        整条链路不因为最外面一层坏了就全废。
        """
        collected = [e for s in steps for e in s.executions if not e.is_error]
        if not collected:
            return f"诊断未能完成：{err}"

        lines = [
            "（模型服务不可用，以下为已经采集到的原始数据，未做分析）",
            "",
            f"原因：{err}",
            "",
        ]
        for e in collected:
            lines.append(f"· {e.call.name}：{e.brief(400)}")
        return "\n".join(lines)

    def _collect_usage(self, diagnosis: Diagnosis) -> None:
        model = self._model
        if hasattr(model, "total_input_tokens"):
            diagnosis.input_tokens = int(getattr(model, "total_input_tokens", 0))
            diagnosis.output_tokens = int(getattr(model, "total_output_tokens", 0))


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


def diagnose(question: str, **kwargs: Any) -> Diagnosis:
    """一行调用：`diagnose("网络很卡")`。"""
    return NetworkAgent(**kwargs).run(question)


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()

    # 本文件的自检：用假模型 + 假工具跑通循环本体，
    # 证明"循环 + 容错 + 轨迹渲染"不依赖 D-Bus、不依赖网络、不花钱。
    # 真实跑法见 main.py。
    # 注意模块名是 offline_demo 而不是 demo —— rag/ 目录里已经有一个 demo.py，
    # 而 rag/ 在 sys.path 上，重名会被它盖掉（踩过）。
    from offline_demo import DemoToolRunner, default_demo_script
    from llm_client import ScriptedChatModel

    agent = NetworkAgent(
        model=ScriptedChatModel(default_demo_script()),
        runner=DemoToolRunner(),
    )
    result = agent.run("网络很卡，帮我看看是什么问题")

    print(result.trace())
    print("=" * 60)
    print(
        f"轮次 {result.rounds}，工具调用 {result.tool_calls} 次，"
        f"结束原因 {result.stopped_because}"
    )
    print("=" * 60)
    print(result.answer)
