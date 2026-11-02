"""
Agent 行为评测 —— 这是 CI 里 test_agent.py（脚本模型、验循环逻辑）之外的另一层。

===========================================================================
为什么有这个文件（它和 test_agent.py / eval_recall.py 的关系）
===========================================================================

  本项目其实已经有两层评测，但都各有盲区：

    · eval_recall.py  —— 测【检索层】召回率（query → 相关 chunk），用谓词标注。
                         但它只管 RAG 把不把对的 chunk 召回来，不管 Agent 最终行为。
    · test_agent.py   —— 测【循环逻辑】的确定性边界（工具失败回灌、重复拦截、
                          轮次上限、上下文裁剪配对、取消），但用的是 ScriptedChatModel
                         假模型，验证的是“代码逻辑对不对”，不是“真实模型下会不会抽风”。

  这一层（eval_agent.py）补的是：用【真实模型】喂一批 query（清晰 / 模糊 / 超纲混搭），
  看 Agent 在真实推理下会不会：
      1) 路径震荡 —— 同一 (工具, 参数) 反复调，或工具间乒乓没进展；
      2) 瞎编答案 —— 最终结论出现工具返回里根本没有的数据 / 指标；
      3) 完不成   —— 卡在轮次上限 / 工具上限，或被迫逼出结论（forced_conclusion）。

  打分是量化的，不是“看反响”：每条 query 出一个分数行，最后有汇总。
  注意：本目录只有十几条 query，数字【有指示性但没统计意义】，真正结论要等
  你把真实诊断日志攒多了再跑。

===========================================================================
用法
===========================================================================

  # 离线冒烟（脚本模型 + 假工具，不烧 Key、不联网，验证评测本身能跑）：
  PYTHONPATH=".:agent" python agent/eval_agent.py --model demo

  # 真实模型评测（需要 QWEN_API_KEY；--runner fake 用假数据喂模型，专心测 Agent）：
  PYTHONPATH=".:agent" python agent/eval_agent.py --model real --runner fake

  # 真实模型 + 真实工具（需要 C++ 服务 / D-Bus 在跑，最贴近生产但最重）：
  PYTHONPATH=".:agent" python agent/eval_agent.py --model real --runner real

  # 用自定义 query 集（JSON：[{"q": "...", "tag": "ambiguous"}, ...]）：
  PYTHONPATH=".:agent" python agent/eval_agent.py --model real --queries my_queries.json

设计说明：真实模型评测【非确定 + 烧 token】，所以只在本地 / ad-hoc 跑，
绝不进 CI（CI 用的是 test_agent.py 的脚本模型）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import Any

from agent import AgentLimits, Diagnosis, NetworkAgent
from llm_client import AnthropicChatModel, ScriptedChatModel, ToolCall, Turn
from tools import TOOLS, ToolRunner


# ---------------------------------------------------------------------------
# 默认评测 query 集：三类混搭，专门暴露不同失败模式
# ---------------------------------------------------------------------------
DEFAULT_QUERIES: list[dict[str, str]] = [
    # —— 清晰：意图明确，正常链路应该走得顺 ——
    {"q": "网络很卡，帮我看看", "tag": "clear"},
    {"q": "wlan0 的丢包率是多少？", "tag": "clear"},
    {"q": "帮我 ping 一下 8.8.8.8", "tag": "clear"},
    # —— 模糊：信息不全，看 Agent 会不会追问题 / 不乱编 ——
    {"q": "我的网好像有点问题", "tag": "ambiguous"},
    {"q": "为什么刷视频会卡？", "tag": "ambiguous"},
    {"q": "帮我优化一下网络", "tag": "ambiguous"},
    {"q": "最近打游戏延迟特别高", "tag": "ambiguous"},
    # —— 超纲：看 Agent 会不会硬装懂 / 是否优雅降级 ——
    {"q": "帮我写一首关于 WiFi 的诗", "tag": "oos"},
    {"q": "今天北京天气怎么样？", "tag": "oos"},
    {"q": "能教我怎么减肥吗？", "tag": "oos"},
]


# ---------------------------------------------------------------------------
# 假工具运行器（--runner fake）：返回贴近真实的固定数据，让真实模型有“证据”可据
# ---------------------------------------------------------------------------
class FakeRunner:
    """不连 D-Bus / C++ 服务，按工具名返回一份像样的样例数据。

    用途：在“真实模型 + 假数据”下评测 Agent 的【行为】（震荡 / 瞎编），
    而不必起整套诊断栈。幻觉检测会把答案里的指标和这里的证据对账。
    """

    def run(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        if name == "get_network_health":
            return {"result": {"quality_score": 64, "issues": ["丢包", "高延迟"],
                               "interfaces": ["eth0", "wlan0"]}}
        if name == "get_conn_stats":
            return {"result": {"rtt_ms": 210, "loss_pct": 3.5, "throughput_mbps": 52.3}}
        if name == "get_flows":
            return {"result": {"flow_count": 3,
                               "top_flows": [{"dest": "8.8.8.8", "bytes": 1024000}]}}
        if name == "ping_host":
            return {"result": {"host": arguments.get("hostname"), "alive": True, "rtt_ms": 35}}
        if name == "get_routing":
            return {"result": {"default_gw": "192.168.1.1"}}
        return {"result": {"note": "ok", "echo": name}}

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return TOOLS

    def bind_memory(self, memory: Any) -> None:  # noqa: D401 - 假运行器无需绑定
        pass


# ---------------------------------------------------------------------------
# 离线 demo 用的脚本模型：一条“采集 → 结论”的固定两回合，零 Key、零网络
# ---------------------------------------------------------------------------
def _call(name: str, **args: Any) -> Turn:
    return Turn(text="", tool_calls=[ToolCall(id=name, name=name, arguments=args)],
                stop_reason="tool_use")


def _final(text: str) -> Turn:
    return Turn(text=text, stop_reason="end_turn")


DEMO_SCRIPT: list[Turn] = [
    # 演示“干净 Agent”：先采健康度，再采连接质量（把 RTT 也拿到），最后给结论。
    # 答案里提到的指标必须和证据对得上，否则会被幻觉检测误报 —— 这里刻意保持一致。
    _call("get_network_health"),
    _call("get_conn_stats"),
    _final("基于采集：质量分 64，RTT 210ms，主要问题是丢包，建议检查链路。"),
]


# ---------------------------------------------------------------------------
# 打分器
# ---------------------------------------------------------------------------
def _evidence_text(diag: Diagnosis) -> str:
    """把所有工具返回拼成“证据文本”，用来对账答案里是否编造指标。"""
    parts: list[str] = [diag.question]
    for step in diag.steps:
        for exec_ in step.executions:
            parts.append(json.dumps(exec_.result, ensure_ascii=False))
    return " ".join(parts)


# 只抓“数字 + 单位”这类最容易被编造的指标（延迟 / 吞吐 / 百分比 / 丢包率），
# 普通计数（如“2 个工具”）不在范围内，避免误报。
_METRIC_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(ms|Mbps|MBps|kbps|MB|GB|%|百分比|丢包率)",
    re.IGNORECASE,
)


def _unbacked_metrics(answer: str, evidence: str) -> list[str]:
    """答案里出现的、证据文本里找不到的指标值。软信号，需人工复核。"""
    flagged: list[str] = []
    for m in _METRIC_RE.finditer(answer or ""):
        if m.group(1) not in evidence:
            flagged.append(m.group(0))
    return flagged


def _max_identical(diag: Diagnosis) -> tuple[int, str]:
    """出现次数最多的 (工具, 参数) 指纹及其次数 —— 超过阈值即震荡嫌疑。"""
    counter: Counter[str] = Counter()
    for step in diag.steps:
        for exec_ in step.executions:
            fp = exec_.call.name + "|" + json.dumps(
                exec_.call.arguments, sort_keys=True, ensure_ascii=False
            )
            counter[fp] += 1
    if not counter:
        return 0, ""
    fp, n = counter.most_common(1)[0]
    return n, fp


def score_one(diag: Diagnosis, limits: AgentLimits) -> dict[str, Any]:
    """把一次 Diagnosis 打成一行分数。"""
    interventions = sum(
        1 for s in diag.steps for e in s.executions if e.intervention
    )
    max_identical, top_fp = _max_identical(diag)
    evidence = _evidence_text(diag)

    completed = (
        diag.stopped_because == "end_turn"
        and diag.answer_is_model_output
        and not diag.forced_conclusion
    )
    errored = diag.error is not None or diag.stopped_because == "llm_error"
    oscillation = (
        max_identical > limits.max_identical_calls
        or diag.stopped_because in ("max_rounds", "max_tool_calls")
    )
    hallucination = _unbacked_metrics(diag.answer, evidence)

    flags: list[str] = []
    if not completed and not errored:
        flags.append("未自然完成")
    if errored:
        flags.append("报错")
    if oscillation:
        flags.append(f"震荡(同指纹最多{max_identical}次)")
    if hallucination:
        flags.append(f"疑似编造({len(hallucination)}个指标)")

    return {
        "completed": completed,
        "errored": errored,
        "oscillation": oscillation,
        "forced": diag.forced_conclusion,
        "hallucination": bool(hallucination),
        "hallucination_detail": hallucination,
        "rounds": diag.rounds,
        "tool_calls": diag.tool_calls,
        "interventions": interventions,
        "max_identical": max_identical,
        "top_fp": top_fp,
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_model(mode: str, args: argparse.Namespace):
    if mode == "demo":
        return ScriptedChatModel(DEMO_SCRIPT)
    # real / auto
    api_key = os.environ.get("QWEN_API_KEY")
    if mode == "auto" and not api_key:
        print("⚠️ 未检测到 QWEN_API_KEY，回退到 demo（脚本模型）模式。")
        return ScriptedChatModel(DEMO_SCRIPT)
    if args.api_key:
        os.environ["QWEN_API_KEY"] = args.api_key
    return AnthropicChatModel(model=args.model_name)


def load_queries(path: str | None) -> list[dict[str, str]]:
    if not path:
        return DEFAULT_QUERIES
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit("query 文件必须是 JSON 数组：[{'q': ..., 'tag': ...}, ...]")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="NetPulse Agent 行为评测")
    parser.add_argument("--model", choices=["auto", "real", "demo"], default="auto",
                        help="auto=有 Key 用真模型否则 demo；real=强制真模型；demo=脚本模型")
    parser.add_argument("--runner", choices=["fake", "real"], default="fake",
                        help="fake=假数据(默认，专心测 Agent)；real=真实工具(需服务在跑)")
    parser.add_argument("--queries", default=None, help="自定义 query JSON 路径")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（抽样调试用）")
    parser.add_argument("--api-key", default=None, help="临时设置 QWEN_API_KEY")
    parser.add_argument("--model-name", default=None,
                        help="覆盖真实模型的 model 名（默认用 llm_client 里的百炼默认）")
    parser.add_argument("--print-traces", action="store_true",
                        help="对所有 query 都打印推理轨迹（默认只打印可疑项）")
    args = parser.parse_args()

    model = build_model(args.model, args)
    runner = FakeRunner() if args.runner == "fake" else ToolRunner()
    limits = AgentLimits()
    agent = NetworkAgent(model=model, runner=runner, limits=limits, use_memory=False)

    queries = load_queries(args.queries)
    if args.limit:
        queries = queries[: args.limit]

    print("=" * 78)
    print("NetPulse Agent 行为评测")
    print(f"  模型: {type(model).__name__}    工具: {args.runner}    "
          f"query 数: {len(queries)}")
    print("=" * 78)

    rows: list[dict[str, Any]] = []
    for i, item in enumerate(queries, 1):
        q = item["q"]
        tag = item.get("tag", "?")
        # 假模型是“有状态的剧本播放器”：每条 query 前拨回开头，
        # 否则第一条把剧本消耗完，后面的 query 收不到工具调用（见 llm_client 的 reset）。
        # 真实模型没有 reset，hasattr 自然跳过。
        if hasattr(model, "reset"):
            model.reset()
        try:
            diag = agent.run(q)
        except Exception as exc:  # 评测本身不应因单次失败而中断
            print(f"[{i}] ❌ 运行异常：{exc}")
            rows.append({"tag": tag, "score": {"flags": [f"异常:{exc}"], "rounds": 0,
                                               "tool_calls": 0}, "diag": None, "q": q})
            continue

        sc = score_one(diag, limits)
        rows.append({"tag": tag, "score": sc, "diag": diag, "q": q})

        status = "✅" if sc["completed"] else ("⚠️ " if (sc["oscillation"] or sc["hallucination"]) else "❌")
        flag_txt = "；".join(sc["flags"]) or "完成"
        print(f"[{i}] {status} [{tag}] 轮次={sc['rounds']} 工具={sc['tool_calls']} "
              f"重复拦截={sc['interventions']} | {flag_txt}")
        if args.print_traces or sc["flags"]:
            print("    └─ " + (diag.answer or "(空结论)").replace("\n", " ").strip()[:120])

    # ---- 汇总 ----
    n = len(rows)
    completed = sum(1 for r in rows if r["score"].get("completed"))
    errored = sum(1 for r in rows if r["score"].get("errored"))
    osc = sum(1 for r in rows if r["score"].get("oscillation"))
    hall = sum(1 for r in rows if r["score"].get("hallucination"))
    forced = sum(1 for r in rows if r["score"].get("forced"))
    print()
    print("-" * 78)
    print(f"汇总：{n} 条 | 自然完成 {completed} | 被迫结论 {forced} | "
          f"报错 {errored} | 震荡嫌疑 {osc} | 疑似编造 {hall}")
    if osc or hall:
        print("⚠️ 出现震荡 / 编造嫌疑 —— 这恰恰说明评测在起作用：去上面逐条看轨迹定位。")
    else:
        print("✅ 本批未见明显震荡 / 编造（样本小，结论仅供参考）。")
    print("-" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
