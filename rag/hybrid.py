"""
混合检索 —— 把向量召回和 BM25 召回融合成一路。

===========================================================================
融合为什么要用 RRF，而不是把分数加权平均？
===========================================================================

最直觉的做法是加权求和：

    score = α * 向量相似度 + (1 - α) * BM25 分数

但这里有个致命问题：**两路的分数根本不在一个量纲上。**

    向量余弦相似度   范围 [0, 1]（归一化后），大量集中在 0.6~0.9，
                     差 0.05 就已经是明显差距了
    BM25 分数        范围 [0, +∞)，常见值 2~15，取决于语料长度和词频，
                     换个语料整体尺度就变了

硬加权的话，α 得跟着语料反复调，换一批数据就失效。这叫**分数不可比**。

RRF（Reciprocal Rank Fusion）绕开了这个问题：
**它只用排名，不用分数。**

    RRF(d) = Σ_r  1 / (k + rank_r(d))

    rank_r(d)  文档 d 在第 r 路召回里的名次（从 1 开始）
    k          平滑常数，论文推荐 60

为什么它好：
  - 只看名次，所以两路分数有多不可比都无所谓
  - k=60 让"第 1 名"和"第 3 名"的差距不至于过于悬殊：
    1/61 = 0.0164  vs  1/63 = 0.0159，差 3%
    如果没有 k（k=0）：1/1 = 1.0  vs  1/3 = 0.33，差 3 倍 —— 第一名一票否决
  - 一个文档被两路同时召回时分数叠加，这正是我们要的"两路都认为相关"

这个方法是 2009 年 Cormack 等人提出的，至今是工业界混合检索的默认方案，
因为**它几乎没有超参要调**。

===========================================================================
完整链路
===========================================================================

                    ┌─────────────┐
    query ──┬──────>│ 向量召回 20 │──┐
            │       └─────────────┘  │   ┌────────┐   ┌────────┐
            │       ┌─────────────┐  ├──>│  RRF   │──>│ Rerank │──> top 5
            └──────>│ BM25 召回 20│──┘   │ 融合   │   │ 精排   │
                    └─────────────┘      └────────┘   └────────┘
                        召回阶段            融合阶段     重排阶段
                     （便宜、粗、要全）              （贵、准、要精）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from bm25 import BM25Index
from chunking import Chunk
from embedder import Embedder
from rerank import NoOpReranker, Reranker
from vector_store import VectorStore

import config


@dataclass
class Hit:
    """一条检索结果，带完整的溯源信息。

    debug 里存了"它在每一路的名次"，这不是装饰 ——
    eval_recall.py 靠它分析"哪些 query 是 BM25 救回来的、
    哪些是 RRF 反而搞坏的"。没有这个，调优就是瞎猜。
    """

    chunk: Chunk
    score: float
    source: str
    debug: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.chunk.text


def rrf_fuse(
    rankings: Sequence[Sequence[str]], k: int = 60
) -> dict[str, float]:
    """Reciprocal Rank Fusion。

    rankings 是若干路召回的结果，每一路是 chunk_id 按名次排好的列表。
    返回 {chunk_id: 融合分数}。
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


def weighted_fuse(
    rankings: Sequence[tuple[Sequence[str], Sequence[float]]],
    alpha: float = 0.5,
) -> dict[str, float]:
    """加权融合（备选方案，用来做对照实验）。

    注意它对分数做了 min-max 归一化 —— 这是补救"量纲不可比"的常见手法，
    但归一化对异常值敏感：一路里如果有个分数特别离谱的，其他全被压扁。
    所以默认不用它，只在评测里做对照，用数据说明 RRF 更好。
    """
    fused: dict[str, float] = {}
    for (ids, raw_scores), weight in zip(rankings, (alpha, 1 - alpha)):
        ids, raw_scores = list(ids), list(raw_scores)
        if not ids:
            continue
        lo, hi = min(raw_scores), max(raw_scores)
        span = (hi - lo) or 1.0
        for cid, score in zip(ids, raw_scores):
            fused[cid] = fused.get(cid, 0.0) + weight * (score - lo) / span
    return fused


class HybridRetriever:
    """混合检索器。同时持有稠密索引、稀疏索引和重排器。"""

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        reranker: Reranker | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker or NoOpReranker()

        self._by_id: dict[str, Chunk] = {c.chunk_id: c for c in store.chunks}

        # BM25 索引从同一批 chunk 建，保证两路看到的是同一份文档集
        self.sparse = BM25Index(
            doc_ids=[c.chunk_id for c in store.chunks],
            doc_texts=[c.text for c in store.chunks],
        )

    # ------------------------------------------------------------------
    # 单路召回（评测要单独跑，所以暴露出来）
    # ------------------------------------------------------------------

    def dense_only(self, query: str, top_k: int = 20) -> list[Hit]:
        vec = self.embedder.embed_query(query)
        return [
            Hit(chunk=c, score=s, source="dense", debug={"dense_rank": i})
            for i, (c, s) in enumerate(self.store.search(vec, top_k), start=1)
        ]

    def sparse_only(self, query: str, top_k: int = 20) -> list[Hit]:
        return [
            Hit(chunk=self._by_id[cid], score=s, source="sparse", debug={"sparse_rank": i})
            for i, (cid, s) in enumerate(self.sparse.search(query, top_k), start=1)
            if cid in self._by_id
        ]

    # ------------------------------------------------------------------
    # 融合
    # ------------------------------------------------------------------

    def hybrid(
        self,
        query: str,
        top_k: int = 20,
        method: str = "rrf",
        weight: float = 0.5,
    ) -> list[Hit]:
        """双路召回 + 融合。"""
        dense = self.dense_only(query, config.DENSE_TOP_K)
        sparse = self.sparse_only(query, config.SPARSE_TOP_K)

        dense_ids = [h.chunk.chunk_id for h in dense]
        sparse_ids = [h.chunk.chunk_id for h in sparse]
        dense_rank = {cid: i for i, cid in enumerate(dense_ids, start=1)}
        sparse_rank = {cid: i for i, cid in enumerate(sparse_ids, start=1)}

        if method == "rrf":
            fused = rrf_fuse([dense_ids, sparse_ids], k=config.RRF_K)
        elif method == "weighted":
            fused = weighted_fuse(
                [(dense_ids, [h.score for h in dense]),
                 (sparse_ids, [h.score for h in sparse])],
                alpha=weight,
            )
        else:
            raise ValueError(f"未知融合方式: {method}")

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]

        return [
            Hit(
                chunk=self._by_id[cid],
                score=score,
                source="hybrid",
                debug={
                    "dense_rank": dense_rank.get(cid),   # None 表示向量那路没召回
                    "sparse_rank": sparse_rank.get(cid),
                    "fusion": method,
                },
            )
            for cid, score in ranked
            if cid in self._by_id
        ]

    # ------------------------------------------------------------------
    # 重排
    # ------------------------------------------------------------------

    def hybrid_rerank(
        self,
        query: str,
        top_k: int = 5,
        candidates: int = 20,
        method: str = "rrf",
    ) -> list[Hit]:
        """完整链路：双路召回 -> RRF 融合 -> Rerank 精排。"""
        fused = self.hybrid(query, top_k=candidates, method=method)
        if not fused:
            return []

        reranked = self.reranker.rerank(query, [h.chunk for h in fused], top_k)
        if not reranked:
            return fused[:top_k]

        # 把融合阶段的名次带过来，方便对比"重排前后谁上来了、谁掉下去了"
        pre_rank = {h.chunk.chunk_id: i for i, h in enumerate(fused, start=1)}
        fused_rank = {h.chunk.chunk_id: h.debug for h in fused}

        return [
            Hit(
                chunk=item.chunk,
                score=item.score,
                source="rerank",
                debug={
                    **fused_rank.get(item.chunk.chunk_id, {}),
                    "pre_rerank_rank": pre_rank.get(item.chunk.chunk_id),
                },
            )
            for item in reranked
        ]

    # ------------------------------------------------------------------

    def retrieve(self, query: str, top_k: int | None = None) -> list[Hit]:
        """按 config 的默认策略检索 —— 上层（Agent / pipeline）只调这个。"""
        return self.hybrid_rerank(
            query,
            top_k=top_k or config.FINAL_TOP_K,
            candidates=config.RERANK_CANDIDATES,
        )


def format_hits(hits: Sequence[Hit], max_chars: int = 600) -> str:
    """把检索结果拼成给 LLM 的上下文。

    两条规矩：
      1. 带上 chunk_id，让模型能引用（"根据 log_ab12"），也让日志可追溯
      2. 截断长度 —— 不截断的话一个小问题可能塞进去几千 token，
         这是 RAG 成本失控最常见的原因
    """
    if not hits:
        return "（没有检索到相关资料）"

    blocks: list[str] = []
    for i, hit in enumerate(hits, start=1):
        text = hit.text
        if len(text) > max_chars:
            text = text[:max_chars] + "…（已截断）"
        meta = hit.chunk.metadata
        origin = meta.get("time_window") or meta.get("topic") or meta.get("source")
        blocks.append(f"【资料 {i}】{hit.chunk.chunk_id}（{origin}）\n{text}")
    return "\n\n".join(blocks)
