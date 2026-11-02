"""
FAISS 稠密检索 —— 向量这一路召回。

===========================================================================
为什么用 IndexFlatIP 而不是 IndexIVFFlat / HNSW？
===========================================================================

因为**你的数据量根本不需要近似索引**。

FAISS 的索引分两类：
  精确索引 (Flat)     暴力比对全部向量，结果 100% 准确，O(N)
  近似索引 (IVF/HNSW) 先聚类再搜，快，但会漏（召回率 <100%）

近似索引的存在意义是"千万级向量毫秒返回"。你现在是几百个 chunk，
Flat 的耗时是微秒级 —— 上 IVF 只会白白损失召回率，还要多调 nprobe。

**面试时这是个加分点**：能说清"我为什么不用更高级的索引"，
比"我用了最先进的索引"更能说明你懂取舍。数据量到几十万再换不迟。

IndexFlatIP 里的 IP = Inner Product（内积）。
配合 embedder 里的 L2 归一化，内积 == 余弦相似度。
"""

from __future__ import annotations

import json
import os
from typing import Sequence

import numpy as np

from chunking import Chunk
from embedder import Embedder


class VectorStore:
    """FAISS 向量库 + chunk 元数据的绑定。

    FAISS 只存向量，不存文本。所以必须自己维护
    「第 i 行向量对应哪个 chunk」的映射 —— 这是初学者最容易漏的一步。
    """

    def __init__(self, dim: int) -> None:
        import faiss

        self.dim = dim
        self._index = faiss.IndexFlatIP(dim)
        self._chunks: list[Chunk] = []

    # ---------------- 建索引 ----------------

    def add(self, chunks: Sequence[Chunk], embedder: Embedder) -> None:
        chunks = list(chunks)
        if not chunks:
            return
        matrix = embedder.embed_documents([c.text for c in chunks])
        if matrix.shape[1] != self.dim:
            raise ValueError(
                f"向量维度不匹配：索引是 {self.dim} 维，embedding 返回 {matrix.shape[1]} 维。\n"
                f"如果你改过 QWEN_EMBEDDING_DIM，必须删掉旧索引重建。"
            )
        self._index.add(matrix)
        self._chunks.extend(chunks)

    # ---------------- 检索 ----------------

    def search(self, query_vector: np.ndarray, top_k: int = 20) -> list[tuple[Chunk, float]]:
        if self._index.ntotal == 0:
            return []
        query = np.asarray(query_vector, dtype="float32").reshape(1, -1)
        scores, indices = self._index.search(query, min(top_k, self._index.ntotal))

        hits: list[tuple[Chunk, float]] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:  # FAISS 用 -1 填充"没搜到"
                continue
            hits.append((self._chunks[idx], float(score)))
        return hits

    # ---------------- 落盘 ----------------

    def save(self, directory: str) -> None:
        import faiss

        os.makedirs(directory, exist_ok=True)
        faiss.write_index(self._index, os.path.join(directory, "dense.faiss"))
        with open(os.path.join(directory, "chunks.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "dim": self.dim,
                    "chunks": [
                        {"chunk_id": c.chunk_id, "text": c.text, "metadata": c.metadata}
                        for c in self._chunks
                    ],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    @classmethod
    def load(cls, directory: str) -> "VectorStore":
        import faiss

        index_path = os.path.join(directory, "dense.faiss")
        meta_path = os.path.join(directory, "chunks.json")
        if not (os.path.exists(index_path) and os.path.exists(meta_path)):
            raise FileNotFoundError(f"{directory} 里没有索引，先跑 build_index.py")

        with open(meta_path, encoding="utf-8") as f:
            payload = json.load(f)

        store = cls(dim=payload["dim"])
        store._index = faiss.read_index(index_path)
        store._chunks = [
            Chunk(chunk_id=c["chunk_id"], text=c["text"], metadata=c["metadata"])
            for c in payload["chunks"]
        ]
        return store

    @property
    def chunks(self) -> list[Chunk]:
        return list(self._chunks)

    def __len__(self) -> int:
        return self._index.ntotal
