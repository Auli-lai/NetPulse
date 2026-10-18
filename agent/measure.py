"""
测量真实诊断的统计量 —— 主要是"单次诊断平均调用几个工具"。

    python measure.py --runs 3 --demo      # 先离线验证脚本本身
    python measure.py --runs 10            # 真实跑 10 次（花钱，会先确认）

===========================================================================
为什么需要这个
===========================================================================

简历上写"单次诊断平均调用 X 个工具"，这个 X 必须是**量出来的**，
不能是估的。面试官追问一句"这个数怎么来的"，答不上来比不写还糟。

而且它确实只能量：
  · 模型每轮调几个工具是它自己决定的，不是代码写死的
  · 同样的问法，两次跑出来的调用次数可能不同
  · 工具报错时它还会重试或换工具，次数会变

所以这个脚本跑 N 次诊断，把每次的轮次、工具调用次数、用到的工具统计出来，
最后给出平均值和分布。

===========================================================================
输出里值得看的几件事
===========================================================================

  · **平均调用次数** —— 就是简历上要填的数
  · **分布** —— 如果每次都是 5，说明你的提示词把行为约束死了；
    如果有 3 有 8，说明模型真的在按情况决策，这个故事更好讲
  · **工具使用频次** —— 哪些工具真被用到、哪些从没被调用过。
    从没被调用的那个，八成是 description 写得让人看不懂该什么时候用
  · **失败率** —— 有多少次没得出结论。这个数字高说明环境或提示词有问题

最后一项特别有用：**一个从没被调用过的工具，等于没做。**
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from collections import Counter
from typing import Any

# ---------------------------------------------------------------------------
# 真实诊断用的问法。
# 刻意覆盖几种不同的诉求 —— 只看一种问法，量出来的平均没有代表性。
# ---------------------------------------------------------------------------
QUESTIONS = [
    "网络很卡，帮我看看是什么问题",
    "eth0 最近是不是有问题",
    "10:02 前后发生了什么",
    "我怀疑有人在下载东西占带宽",
    "WiFi 信号弱该怎么处理",
    "RTT 多少算严重",
    "网络断断续续的，帮我排查一下",
    "为什么我的延迟这么高",
    "最近有没有出现过丢包",
    "帮我看看现在的网络健康情况",
]


def build_agent(demo: bool, use_memory: bool):
    from agent import AgentLimits, NetworkAgent

    if demo:
        from llm_client import ScriptedChatModel
        from offline_demo import DemoToolRunner, default_demo_script

        return NetworkAgent(
            model=ScriptedChatModel(default_demo_script()),
            runner=DemoToolRunner(),
            use_memory=False,
        )

    from tools import ToolRunner

    return NetworkAgent(
        model=None,
        runner=ToolRunner(),
        use_memory=use_memory,
        limits=AgentLimits(),
    )


def measure(runs: int, demo: bool, use_memory: bool, quiet: bool) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    tool_counter: Counter[str] = Counter()

    for i in range(runs):
        question = QUESTIONS[i % len(QUESTIONS)]
        agent = build_agent(demo=demo, use_memory=use_memory)
        t0 = time.time()
        result = agent.run(question)
        elapsed = time.time() - t0

        used = [e.call.name for s in result.steps for e in s.executions]
        tool_counter.update(used)

        rows.append(
            {
                "question": question,
                "rounds": result.rounds,
                "tool_calls": result.tool_calls,
                "stopped": result.stopped_because,
                "ok": bool(result.answer_is_model_output),
                "seconds": round(elapsed, 1),
                "tools": used,
            }
        )
        if not quiet:
            mark = "OK " if result.answer_is_model_output else "FAIL"
            print(
                f"  [{i + 1:2d}/{runs}] {mark}  轮次={result.rounds}  "
                f"工具={result.tool_calls}  {elapsed:5.1f}s  "
                f"{result.stopped_because:14s} {question[:22]}"
            )

    calls = [r["tool_calls"] for r in rows]
    rounds = [r["rounds"] for r in rows]
    ok_count = sum(1 for r in rows if r["ok"])

    return {
        "runs": len(rows),
        "rows": rows,
        "avg_tool_calls": statistics.mean(calls) if calls else 0,
        "median_tool_calls": statistics.median(calls) if calls else 0,
        "min_tool_calls": min(calls) if calls else 0,
        "max_tool_calls": max(calls) if calls else 0,
        "avg_rounds": statistics.mean(rounds) if rounds else 0,
        "success_rate": ok_count / len(rows) if rows else 0,
        "tool_counter": tool_counter,
    }


def main() -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rag"))
    from console import enable_utf8_output

    enable_utf8_output()

    p = argparse.ArgumentParser(
        description="测量真实诊断的统计量（简历上那个『平均调用 N 个工具』要用它量）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--runs", type=int, default=10, help="跑几次，默认 10")
    p.add_argument("--demo", action="store_true", help="用假模型跑（不花钱，用来验证脚本本身）")
    p.add_argument("--no-memory", action="store_true", help="测量时不读写长期记忆")
    p.add_argument("--yes", action="store_true", help="跳过花钱确认")
    p.add_argument("--quiet", action="store_true", help="不逐条打印")
    args = p.parse_args()

    if not args.demo and not args.yes:
        # 真实诊断每次要调好几次模型，是真金白银。先问一句。
        print(f"即将真实跑 {args.runs} 次诊断。每次约 3~8 次模型调用。")
        print("这会消耗百炼额度。加 --yes 可以跳过这个确认。")
        if input("继续？(yes/no): ").strip().lower() != "yes":
            print("已取消。")
            return 0

    print(f"\n跑 {args.runs} 次{'（演示模式，不花钱）' if args.demo else ''}…\n")
    stats = measure(args.runs, args.demo, not args.no_memory, args.quiet)

    print("\n" + "=" * 68)
    print("结果")
    print("=" * 68)
    print(f"  样本数            {stats['runs']}")
    print(f"  平均工具调用次数   {stats['avg_tool_calls']:.2f}   ← 简历上填这个")
    print(f"  中位数            {stats['median_tool_calls']:.1f}")
    print(f"  范围              {stats['min_tool_calls']} ~ {stats['max_tool_calls']}")
    print(f"  平均推理轮次       {stats['avg_rounds']:.2f}")
    print(f"  成功率            {stats['success_rate'] * 100:.0f}%")

    print("\n  工具使用频次（哪些真被用到）：")
    for name, n in stats["tool_counter"].most_common():
        bar = "█" * max(1, round(n / max(1, stats["runs"]) * 20))
        print(f"    {name:26s} {n:3d}  {bar}")

    # 从没被调用过的工具 = description 没写好，模型不知道什么时候该用它
    try:
        from tools import TOOL_NAMES

        unused = [t for t in TOOL_NAMES if t not in stats["tool_counter"]]
        if unused:
            print(f"\n  ⚠️  这些工具一次都没被调用过：{unused}")
            print("     要么这次的问题用不到它们，要么 description 没写清『什么时候调』。")
            print("     （一个从没被调用过的工具，等于没做。）")
    except ImportError:
        pass

    if stats["success_rate"] < 1:
        failed = [r for r in stats["rows"] if not r["ok"]]
        print(f"\n  ⚠️  {len(failed)} 次没得出结论，结束原因："
              f"{Counter(r['stopped'] for r in failed)}")

    if stats["avg_tool_calls"] < 2:
        print("\n  ⚠️  平均不到 2 次调用 —— 模型没在真正多步排查，检查一下工具描述。")

    print()
    print("把这个数填进简历：『单次诊断平均调用 "
          f"{stats['avg_tool_calls']:.1f} 个工具』")
    return 0


if __name__ == "__main__":
    sys.exit(main())
