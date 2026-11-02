"""
检索评测 —— 这一份是本次改造里**最值钱**的东西。

===========================================================================
为什么必须有它
===========================================================================

"我做了混合检索和 Rerank" 是一句简历话术。
"混合检索把 Recall@5 从 0.60 提到 0.85" 是一条**证据**。

面试官听到第一句会问"那你怎么知道它有效"，
听到第二句会问"你怎么评测的" —— 而后者是你已经准备好的问题。

没有评测，你调参数（RRF 的 k、时间窗长度、rerank 的 top_n）
全靠感觉，改完可能变差了你也不知道。

===========================================================================
怎么在没有标注数据的情况下做评测
===========================================================================

标准做法是人肉标注 (query, 相关 chunk) 对，但你一个人标几十条
太费时间，而且日志一变标注就失效。

这里用**谓词标注**：不写死 chunk_id（那是哈希，重建索引就变），
而是写"什么样的 chunk 算相关"这个规则。

    ("wlan0 信号劣化", lambda c: c.metadata.get("interface") == "wlan0")

好处：
  - 换日志、重切分、换 embedding，评测集都不用改
  - 规则本身可读，等于把"我认为什么算相关"显式写下来了
  - 面试时可以展示：**我知道标准的做法是人工标注，这里用谓词是因为
    标注成本高且语料会变，谓词的代价是我的相关性定义比人粗**

能主动说出方法的局限，比声称方法完美更能体现工程判断力。

===========================================================================
看结果时注意
===========================================================================

本目录只有几十个 chunk、十来条 query，所以数字**有指示性但没统计意义**。
真正的结论要等你的日志攒到几百个 chunk。
把这点说出来，比假装数字很权威要专业。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Callable

import config
from chunking import Chunk
from hybrid import HybridRetriever
from pipeline import NetworkRAG

# 相关性谓词：入参是 Chunk，返回 bool
Predicate = Callable[[Chunk], bool]


@dataclass
class EvalCase:
    query: str
    relevant: Predicate
    note: str = ""


def _log(iface: str | None = None, contains: str | None = None) -> Predicate:
    def pred(c: Chunk) -> bool:
        if c.metadata.get("source") != "log":
            return False
        if iface and c.metadata.get("interface") != iface:
            return False
        return contains in c.text if contains else True

    return pred


def _kb(category: str | None = None, topic: str | None = None) -> Predicate:
    def pred(c: Chunk) -> bool:
        if c.metadata.get("source") != "knowledge":
            return False
        if category and c.metadata.get("category") != category:
            return False
        return c.metadata.get("topic") == topic if topic else True

    return pred


# ---------------------------------------------------------------------------
# 评测集
# ---------------------------------------------------------------------------

CASES: list[EvalCase] = [
    EvalCase(
        "eth0 的延迟是什么时候突然升高的",
        _log("eth0", "210ms"),
        "字面特征强，BM25 应该占优",
    ),
    EvalCase(
        "eth0 在 10:30 出现的大流量和严重丢包",
        _log("eth0", "95.0MB/s"),
        "数字是唯一线索，向量容易漏",
    ),
    EvalCase(
        "wlan0 的信号强度一路下降",
        _log("wlan0"),
        "网卡名是硬区分，BM25 占优",
    ),
    EvalCase(
        "网卡连接断开、流量归零是什么表现",
        _log("eth0", "0.0MB/s"),
        "语义描述，向量应该更好",
    ),
    EvalCase(
        "10:00 的时候 eth0 还是正常的",
        _log("eth0", "14ms"),
        "需要精确到数值",
    ),
    EvalCase(
        "RTT 过高有哪些排查方向",
        _kb("rtt_analysis", "troubleshooting"),
        "纯知识库问题，日志里没有答案",
    ),
    EvalCase(
        "TCP 丢包率多高算严重",
        _kb("tcp_loss_analysis"),
        "纯语义，无字面重合",
    ),
    EvalCase(
        "WiFi 信号弱应该怎么处理",
        _kb("rssi_analysis"),
        "中文改写（信号弱 vs RSSI），考语义",
    ),
    EvalCase(
        "网络质量评分多少分算差",
        _kb("quality_assessment"),
        "需要理解评分体系",
    ),
    EvalCase(
        "网络流量异常增长会不会导致拥塞",
        _kb("traffic_analysis"),
        "跨类别语义匹配",
    ),
]


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def evaluate(
    retriever: HybridRetriever,
    cases: list[EvalCase],
    top_k: int,
    mode: str,
) -> dict[str, float]:
    """跑一遍评测集，返回聚合指标。"""
    recall_sum = 0.0
    mrr_sum = 0.0
    hit_sum = 0.0

    for case in cases:
        if mode == "sparse":
            hits = retriever.sparse_only(case.query, top_k=top_k)
        elif mode == "dense":
            hits = retriever.dense_only(case.query, top_k=top_k)
        elif mode == "hybrid":
            hits = retriever.hybrid(case.query, top_k=top_k)
        elif mode == "hybrid_rerank":
            hits = retriever.hybrid_rerank(
                case.query, top_k=top_k, candidates=config.RERANK_CANDIDATES
            )
        else:
            raise ValueError(mode)

        # 命中位置
        ranks = [
            i for i, h in enumerate(hits, start=1) if case.relevant(h.chunk)
        ]
        recall_sum += 1.0 if ranks else 0.0
        hit_sum += 1.0 if ranks else 0.0
        mrr_sum += (1.0 / ranks[0]) if ranks else 0.0

    n = len(cases) or 1
    return {
        "Recall@k": recall_sum / n,
        "Hit@k": hit_sum / n,
        "MRR": mrr_sum / n,
    }


def per_case_report(
    retriever: HybridRetriever, cases: list[EvalCase], top_k: int
) -> None:
    """逐条对比：哪条 query 是 BM25 救回来的，哪条是向量救回来的。

    这张表比总分有用 —— 总分只告诉你"有没有用"，
    这张表告诉你"在什么情况下有用"，后者才能指导优化。
    """
    print()
    print("=" * 96)
    print("逐条分析：每条 query 分别被哪一路召回（✓ = 命中，· = 未命中）")
    print("=" * 96)
    header = f"{'query':34s} {'BM25':>6s} {'向量':>6s} {'混合':>6s} {'+重排':>6s}  说明"
    print(header)
    print("-" * 96)

    for case in cases:
        marks = []
        for mode in ("sparse", "dense", "hybrid", "hybrid_rerank"):
            if mode == "sparse":
                hits = retriever.sparse_only(case.query, top_k=top_k)
            elif mode == "dense":
                hits = retriever.dense_only(case.query, top_k=top_k)
            elif mode == "hybrid":
                hits = retriever.hybrid(case.query, top_k=top_k)
            else:
                hits = retriever.hybrid_rerank(
                    case.query, top_k=top_k, candidates=config.RERANK_CANDIDATES
                )
            marks.append("✓" if any(case.relevant(h.chunk) for h in hits) else "·")

        q = case.query if len(case.query) <= 32 else case.query[:31] + "…"
        # 用中文对齐会很乱，这里用固定宽度近似处理
        pad = 34 - sum(2 if ord(ch) > 127 else 1 for ch in q)
        print(f"{q}{' ' * max(pad, 1)} {marks[0]:>6s} {marks[1]:>6s} {marks[2]:>6s} {marks[3]:>6s}  {case.note}")


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="检索质量评测")
    parser.add_argument("--top-k", type=int, default=5, help="评测时的 top_k，默认 5")
    parser.add_argument("--offline", action="store_true", help="离线模式（结果无意义，仅供调试）")
    parser.add_argument("--no-rerank", action="store_true", help="跳过 rerank（不花 rerank 的钱）")
    args = parser.parse_args()

    offline = args.offline or config.OFFLINE_EMBEDDING

    try:
        rag = NetworkRAG.load(offline=offline, use_rerank=not args.no_rerank)
    except RuntimeError as exc:
        print(exc)
        return 1

    retriever = rag.retriever
    print(f"索引：{len(retriever.store)} 个 chunk")
    print(f"评测集：{len(CASES)} 条 query，top_k = {args.top_k}")
    if offline:
        print()
        print("⚠️  离线模式：向量为哈希伪向量，"
              "「向量」列的分数不代表真实 embedding 的效果。")
    print()

    modes = [("sparse", "仅 BM25（稀疏）"), ("dense", "仅向量（稠密）"),
             ("hybrid", "混合 · RRF 融合"), ("hybrid_rerank", "混合 + Rerank")]
    if args.no_rerank:
        modes = modes[:3]

    results: dict[str, dict[str, float]] = {}
    for mode, label in modes:
        print(f"跑 {label} …")
        results[label] = evaluate(retriever, CASES, args.top_k, mode)

    # ---- 汇总表 ----
    print()
    print("=" * 72)
    print(f"检索质量对比（top_k = {args.top_k}）")
    print("=" * 72)
    print(f"{'方案':<22s} {'Recall@k':>10s} {'Hit@k':>9s} {'MRR':>8s}   相对提升")
    print("-" * 72)

    baseline = results[modes[0][1]]["MRR"]
    for _, label in modes:
        m = results[label]
        lift = (m["MRR"] - baseline) / baseline * 100 if baseline else 0.0
        lift_str = "—" if label == modes[0][1] else f"{lift:+.1f}%"
        print(
            f"{label:<22s} {m['Recall@k']:>10.3f} {m['Hit@k']:>9.3f} "
            f"{m['MRR']:>8.3f}   {lift_str}"
        )

    per_case_report(retriever, CASES, args.top_k)

    print()
    print("=" * 72)
    print("怎么读这张表：")
    print("  · BM25 强在字面（网卡名、数值），向量强在语义（中文改写、同义）")
    print("  · 混合检索的收益在「逐条分析」里看得最清楚：")
    print("    只有两路都能命中的 query，RRF 才有意义；")
    print("    某条 query 单独跑两路都挂、混合后命中了，那才是 1+1>2 的直接证据")
    print("  · 若 rerank 没有提升甚至变差，先看候选集大小：")
    print("    融合后只给 rerank 5 条候选，它能做的就只是换个顺序，救不回漏召回的")
    print()
    print("⚠️  样本量说明：几十个 chunk + 十来条 query，数字有指示性，")
    print("    没有统计显著性。日志攒到几百个 chunk 后再看趋势。")
    return 0


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()
    raise SystemExit(main())
