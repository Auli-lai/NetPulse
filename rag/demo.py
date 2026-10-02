"""
命令行问答 —— 这个是"你要怎么用"的答案。

原来的 AI-assisted analysis 目录里有 8 个分析脚本，各写各的
（simple / optimized / true / vector / local_vector / interactive），
每个都要求你改源码里的 api_key 变量才能跑。这一份是唯一的入口。

用法：

    export QWEN_API_KEY='sk-...'

    python demo.py                       # 交互模式，一问一答
    python demo.py -q "eth0 刚才怎么了"   # 单次提问
    python demo.py --retrieve-only -q "..."  # 只看召回，不调生成（省钱）
    python demo.py --offline             # 无网/无 key 跑通链路
"""

from __future__ import annotations

import argparse
import sys

import config
from pipeline import NetworkRAG


def print_answer(result) -> None:
    print("=" * 70)
    print("召回的资料：")
    for i, hit in enumerate(result.hits, start=1):
        meta = hit.chunk.metadata
        origin = meta.get("time_window") or meta.get("topic") or meta.get("source")
        rank_info = []
        if hit.debug.get("dense_rank"):
            rank_info.append(f"向量第{hit.debug['dense_rank']}")
        if hit.debug.get("sparse_rank"):
            rank_info.append(f"BM25第{hit.debug['sparse_rank']}")
        if hit.debug.get("pre_rerank_rank"):
            rank_info.append(f"重排前第{hit.debug['pre_rerank_rank']}")
        trail = " / ".join(rank_info) or "-"
        # 这一行是混合检索最直观的证据：能看出某条资料是被哪一路捞回来的
        print(f"  {i}. {hit.chunk.chunk_id}  [{origin}]  分数={hit.score:.4f}  （{trail}）")

    print()
    print("回答：")
    print(result.answer)
    if result.error:
        print(f"\n⚠️ 生成降级：{result.error}")
    print("=" * 70)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="NetPulse 网络诊断 RAG 问答")
    parser.add_argument("-q", "--question", help="直接提问，不进交互模式")
    parser.add_argument("--retrieve-only", action="store_true", help="只检索不生成")
    parser.add_argument("--offline", action="store_true", help="离线模式（伪向量，不调 API）")
    parser.add_argument("--no-rerank", action="store_true", help="关闭 rerank，对比效果")
    parser.add_argument("-k", "--top-k", type=int, default=None, help="返回资料条数")
    args = parser.parse_args()

    offline = args.offline or config.OFFLINE_EMBEDDING

    try:
        rag = NetworkRAG.load(offline=offline, use_rerank=not args.no_rerank)
    except RuntimeError as exc:
        print(exc)
        return 1

    print(f"已加载索引：{len(rag.retriever.store)} 个 chunk")
    if offline:
        print("离线模式：向量为哈希伪向量，只有字面匹配能力\n")

    # ---- 单次提问 ----
    if args.question:
        if args.retrieve_only:
            for hit in rag.retrieve(args.question, top_k=args.top_k or 5):
                print(f"{hit.score:.4f}  {hit.chunk.chunk_id}")
                print(hit.text[:300])
                print("-" * 60)
        else:
            print_answer(rag.ask(args.question, top_k=args.top_k))
        return 0

    # ---- 交互模式 ----
    print("输入问题，回车提问；输入 q 退出。\n")
    examples = [
        "10:02 前后 eth0 发生了什么",
        "wlan0 的信号是什么时候开始变差的",
        "RTT 超过多少算严重，应该怎么排查",
        "10:30 这次的流量有什么异常",
    ]
    print("可以试试这些问题：")
    for e in examples:
        print(f"  · {e}")
    print()

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

        if args.retrieve_only:
            for hit in rag.retrieve(question, top_k=args.top_k or 5):
                print(f"{hit.score:.4f}  {hit.chunk.chunk_id}")
            continue

        print_answer(rag.ask(question, top_k=args.top_k))

    return 0


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()
    sys.exit(main())
