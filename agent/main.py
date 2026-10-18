"""
Agent 命令行入口 —— 你要怎么用这个东西。

===========================================================================
两种模式，先从 --dry-run 开始
===========================================================================

    # ① 离线演示：不用 API Key，不用装 dbus，不用起 C++ 服务
    python main.py --dry-run

      跑给一个剧本化的假模型，把"模型思考 → 调工具 → 回灌 → 再思考 → 出结论"
      整条推理链打出来。**第一次接触建议先跑这个** —— 先看清楚循环长什么样，
      再去接真实依赖，比反过来快得多。

    # ② 真实诊断：需要 Key + 编译好的服务
    export QWEN_API_KEY='sk-...'
    python main.py -q "网络很卡，帮我看看"

===========================================================================
跑真实模式前的检查清单
===========================================================================

    1) API Key 配了吗
         export QWEN_API_KEY='sk-...'      （Windows: $env:QWEN_API_KEY='sk-...'）

    2) RAG 索引建了吗（search_network_history 要用）
         cd ../rag && python build_index.py --sample

    3) C++ 服务起了吗（其余工具要用）
         sudo -E ./server/bin/weaknet-dbus-server

    三条缺任何一条都不会让程序崩 —— 对应的工具会返回 error，模型看得到
    并会绕开它。这是有意设计的降级，不是 bug。

===========================================================================
用法
===========================================================================

    python main.py --dry-run                 离线演示完整推理链
    python main.py                           交互模式
    python main.py -q "网络很卡"              单次提问
    python main.py --json -q "..."           输出完整轨迹 JSON（给前端用）
    python main.py --list-tools              看模型能调哪些工具
    python main.py --max-rounds 5 -q "..."   限制推理轮次
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agent import AgentLimits, NetworkAgent
from llm_client import LLMError
from session import Session
from tools import TOOL_NAMES, ToolRunner

# ANSI 颜色。终端不支持时会显示成乱码，所以提供一个关掉的开关。
_C = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "reset": "\033[0m",
}


class Printer:
    """把 Agent 的事件流渲染成人看的推理轨迹。

    这一层就是 Week 4 前端可视化要显示的东西 —— 现在先打在终端里。
    事件名（start / thought / tool_result / finish）和传给 SSE 的完全一样，
    到时把 print 换成 push 就行。
    """

    def __init__(self, verbose: bool = True, use_color: bool = True) -> None:
        self.verbose = verbose
        self.color = use_color

    def _c(self, key: str, text: str) -> str:
        return f"{_C[key]}{text}{_C['reset']}" if self.color else text

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        if not self.verbose:
            return

        if kind == "start":
            print(self._c("dim", f"❯ {payload['question']}"))
            print()

        elif kind == "thought":
            text = (payload.get("text") or "").strip()
            if text:
                print(self._c("bold", f"[第 {payload['round']} 轮]") + f" {text}")

        elif kind == "tool_result":
            name = payload["name"]
            ms = payload.get("elapsed_ms", 0)
            intervention = payload.get("intervention")
            if intervention:
                print(f"  {self._c('yellow', '⛔')} {name}  {self._c('yellow', intervention)}")
            elif payload.get("is_error"):
                print(f"  {self._c('red', '❌')} {name}  {self._c('dim', f'({ms:.0f}ms)')}")
            else:
                print(f"  {self._c('green', '🔧')} {name}  {self._c('dim', f'({ms:.0f}ms)')}")

        elif kind == "final":
            # 结论单独由 run_once 打印（带分隔线），这里不重复输出
            pass

        elif kind == "memory_recall":
            items = payload.get("items") or []
            print(self._c("cyan", f"🧠 想起了 {len(items)} 条过往诊断："))
            for line in items:
                print(self._c("dim", f"     {line}"))

        elif kind == "memory_write":
            print(self._c("dim", "🧠 已记入长期记忆"))

        elif kind == "forcing_conclusion":
            print()
            print(self._c("yellow", "⚠️  达到工具调用上限，正在用已有数据强制收尾…"))

        elif kind == "error":
            print(self._c("red", f"✖ {payload['message']}"))


def build_agent(args: argparse.Namespace) -> NetworkAgent:
    """按命令行参数组装 Agent。"""
    limits = AgentLimits(
        max_rounds=args.max_rounds,
        max_tool_calls=args.max_tool_calls,
    )
    printer = Printer(verbose=not args.quiet, use_color=not args.no_color)

    if args.dry_run:
        # 离线演示：假模型 + 假工具。不碰 API、不碰 D-Bus、不花钱。
        from llm_client import ScriptedChatModel
        from offline_demo import DemoToolRunner, default_demo_script

        return NetworkAgent(
            model=ScriptedChatModel(default_demo_script()),
            runner=DemoToolRunner(),
            limits=limits,
            on_event=printer,
            use_memory=False,  # 演示不写记忆，免得假诊断污染真实记忆库
        )

    return NetworkAgent(
        model=None,  # 懒加载真实模型（缺 Key / 缺 SDK 时会在第一次调用时报错）
        runner=ToolRunner(),
        limits=limits,
        on_event=printer,
        use_memory=not args.no_memory,
    )


def print_tools(use_color: bool = True) -> None:
    runner = ToolRunner()
    print(f"模型可以调用 {len(runner.tools)} 个工具：\n")
    for t in runner.tools:
        required = t.parameters.get("required", [])
        optional = [k for k in t.parameters.get("properties", {}) if k not in required]
        sig = ", ".join(list(required) + [f"{o}=?" for o in optional])
        name = f"{_C['bold']}{t.name}{_C['reset']}" if use_color else t.name
        print(f"  {name}({sig})")
        # description 是给模型看的，比较长，这里只打印第一段（"什么时候调"之前）
        first = t.description.split("【")[0].strip().replace("\n", " ")
        print(f"      {first}\n")


def run_once(
    agent: NetworkAgent, question: str, args: argparse.Namespace, session: Any = None
) -> int:
    result = agent.run(question, session=session)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if not result.error else 1

    sep = "─" * 66
    print()
    print(sep)
    if result.forced_conclusion:
        print("⚠️  结论是在达到上限后强制生成的，依据可能不够充分")
    print(result.answer)
    print(sep)

    if not args.quiet:
        stats = (
            f"轮次 {result.rounds} · 工具调用 {result.tool_calls} 次 · "
            f"结束原因 {result.stopped_because}"
        )
        if result.input_tokens:
            stats += f" · {result.input_tokens}+{result.output_tokens} tokens"
        print(stats)

    if result.error:
        print(f"\n⚠️  {result.error}")
        return 1
    return 0


def main() -> int:
    from console import enable_utf8_output

    enable_utf8_output()

    parser = argparse.ArgumentParser(
        description="NetPulse 网络诊断 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-q", "--question", help="直接提问，不进交互模式")
    parser.add_argument("--dry-run", action="store_true",
                        help="离线演示：假模型 + 假工具，不需要 API Key / dbus / C++ 服务")
    parser.add_argument("--json", action="store_true", help="输出完整轨迹 JSON")
    parser.add_argument("--list-tools", action="store_true", help="列出所有工具后退出")
    parser.add_argument("--quiet", action="store_true", help="只输出结论，不打印推理过程")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    parser.add_argument("--max-rounds", type=int, default=8, help="推理轮次上限，默认 8")
    parser.add_argument("--max-tool-calls", type=int, default=16,
                        help="工具调用总次数上限，默认 16")
    # ---- 记忆 ----
    parser.add_argument("--no-memory", action="store_true",
                        help="关闭长期记忆（不读也不写）")
    parser.add_argument("--memories", action="store_true",
                        help="列出长期记忆库里存了什么，然后退出")
    parser.add_argument("--forget", metavar="MEM_ID",
                        help="删掉某条记忆（id 用 --memories 查）")
    parser.add_argument("--clear-memory", action="store_true",
                        help="清空整个长期记忆库（会二次确认）")
    args = parser.parse_args()

    if args.list_tools:
        print_tools(use_color=not args.no_color)
        return 0

    # 记忆管理的三个命令在这里处理完就退出，不起 Agent
    if args.memories or args.forget or args.clear_memory:
        return handle_memory_cli(args)

    try:
        agent = build_agent(args)
    except LLMError as exc:
        print(exc)
        return 1

    # 提示一下环境缺什么。不阻断运行 —— 工具自己会降级。
    if not args.dry_run and not args.quiet:
        _preflight_warnings()

    if args.question:
        return run_once(agent, args.question, args)

    if args.dry_run:
        # 演示模式没有交互的意义（剧本是固定的），默认问一个问题就跑
        return run_once(agent, "网络很卡，帮我看看是什么问题", args)

    # ---- 交互模式 ----
    # 整个交互过程共用一个 Session，所以第二句能看懂"那 wlan0 呢"。
    # 这也是交互模式和 -q 单次提问的区别所在。
    session = Session()

    print("输入问题，回车提问；输入 q 退出。")
    print("推理过程会实时打印出来 —— 能看到模型每一轮在想什么、调了什么工具。")
    if not args.no_memory:
        print("记忆已开启：会参考过往诊断，也会把这次的结论记下来。")
    print()
    examples = [
        "网络很卡，帮我看看是什么问题",
        "10:02 前后 eth0 发生了什么",
        "WiFi 信号弱该怎么处理",
        "我怀疑有人在下载东西占带宽",
    ]
    print("可以试试这些问题：")
    for e in examples:
        print(f"  · {e}")
    print()

    exit_code = 0
    while True:
        try:
            question = input("问题 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question:
            continue
        if question.lower() in ("q", "quit", "exit"):
            break
        exit_code = run_once(agent, question, args, session=session) or exit_code
        print()
    return exit_code


def handle_memory_cli(args: argparse.Namespace) -> int:
    """--memories / --forget / --clear-memory 的处理。

    为什么要把记忆做成**可查看、可删除**的命令行：AI 记住的东西如果不透明，
    用户就没法信任它。"它为什么这么判断"要能回答，"这条记错了"要能删掉 ——
    这也是 memory.py 用 JSONL 当事实源的原因（一行一条，能读能改）。
    """
    from memory import LongTermMemory

    mem = LongTermMemory()

    if args.clear_memory:
        n = len(mem)
        if n == 0:
            print("记忆库本来就是空的。")
            return 0
        print(f"要清空 {n} 条记忆吗？这个操作不可撤销。")
        # 二次确认：清空是不可逆的，不该一条命令就删掉用户积累的东西
        answer = input("输入 yes 确认：").strip().lower()
        if answer != "yes":
            print("已取消。")
            return 0
        mem.clear()
        print(f"已清空 {n} 条记忆。")
        return 0

    if args.forget:
        if mem.forget(args.forget):
            print(f"已删除记忆 {args.forget}")
            return 0
        print(f"没有找到 id 为 {args.forget} 的记忆。用 --memories 查看现有 id。")
        return 1

    # --memories
    records = mem.all()
    stats = mem.stats()
    if not records:
        print("长期记忆库是空的 —— 还没做过诊断，或者一直是 --no-memory 模式。")
        print(f"存储位置：{stats['path']}")
        return 0

    print(f"共 {len(records)} 条长期记忆（{stats['path']}）\n")
    for r in sorted(records, key=lambda r: r.created_at, reverse=True):
        print(f"  {r.id}")
        print(f"    日期：{r.created_at[:10]}   被召回 {r.recall_count} 次"
              f"{f'   重复出现 {r.occurrences} 次' if r.occurrences > 1 else ''}")
        print(f"    问题：{r.question[:70]}")
        print(f"    结论：{r.conclusion.replace(chr(10), ' ')[:90]}…")
        if r.interfaces:
            print(f"    网卡：{', '.join(r.interfaces)}")
        print()
    print(f"删掉某条：python main.py --forget <id>")
    return 0


def _preflight_warnings() -> None:
    """启动时检查外部依赖，缺什么提前说 —— 别等模型调了才报错。"""
    import os

    missing: list[str] = []

    if not os.environ.get("QWEN_API_KEY"):
        missing.append(
            "QWEN_API_KEY 没设置 —— 模型调用会失败。\n"
            "        export QWEN_API_KEY='sk-...'"
        )

    rag_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rag")
    if not os.path.exists(os.path.join(rag_dir, "index_store", "dense.faiss")):
        missing.append(
            "RAG 索引还没建 —— search_network_history 会用不了。\n"
            "        cd ../rag && python build_index.py --sample"
        )

    try:
        import dbus  # noqa: F401
    except ImportError:
        missing.append(
            "没装 dbus-python —— 实时指标类工具会用不了（只影响 Linux 之外的机器）。\n"
            "        WSL/Ubuntu: sudo apt install python3-dbus"
        )

    if missing:
        print("⚠️  环境检查：以下依赖缺失，对应功能会降级（不影响其它部分）\n")
        for m in missing:
            print(f"  · {m}")
        print()


if __name__ == "__main__":
    sys.exit(main())
