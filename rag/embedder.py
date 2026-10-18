"""
向量化 —— 走百炼的 OpenAI 兼容端点。

===========================================================================
为什么这里不用 Anthropic 协议？
===========================================================================

因为 Anthropic 的 API 协议里**没有 embedding 接口** —— Anthropic 官方
不提供向量化模型。你那个 /apps/anthropic 端点只是把 Anthropic 的
Messages 协议翻译成 Qwen 能懂的格式，它依然只覆盖原本就有的能力。

所以：生成走 Anthropic 协议，向量化必须换到 OpenAI 兼容端点。
这是本套代码里最容易被忽略、但一写就报错的地方。

===========================================================================
为什么要把 embedding 抽象成接口？
===========================================================================

因为向量化是**唯一会花钱、也唯一会失败**的一步。抽象出来有三个好处：

  1. 可以换模型（text-embedding-v3 -> v4）而不动上层代码
  2. 可以换成离线实现，在没网时把整条链路跑通（见 OfflineHashEmbedder）
  3. 可以加缓存：同一段文本重复向量化是纯浪费

第 2 点在面试里很好讲：**"我怎么在 CI 里测一条依赖外部 API 的检索链路"**
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from typing import Sequence

import numpy as np

from config import (
    EMBEDDING_BASE_URL,
    EMBEDDING_BATCH,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    require_api_key,
)


class Embedder(ABC):
    """向量化接口。所有实现都必须返回 L2 归一化后的向量。"""

    dim: int

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """批量向量化。返回 shape=(len(texts), dim)。"""

    def embed_query(self, text: str) -> np.ndarray:
        """查询向量化。默认复用批量实现 —— 但要注意这行代码背后的坑：

        有些 embedding 模型对「查询」和「文档」用不同的编码方式
        （叫 asymmetric embedding，比如 bge 系列要加 "query:" 前缀）。
        text-embedding-v3 是对称的，所以可以复用。
        如果哪天换成 bge，这里必须单独实现，否则召回率会莫名下降。
        """
        return self.embed_documents([text])[0]


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """L2 归一化。

    归一化之后，向量的**内积就等于余弦相似度**。
    这样 FAISS 可以用最简单的 IndexFlatIP（内积索引），不需要
    自己写余弦距离，也不需要担心向量长度影响排序。
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0  # 防零向量除零
    return matrix / norms


# ---------------------------------------------------------------------------
# 真实实现：百炼 text-embedding
# ---------------------------------------------------------------------------


class DashScopeEmbedder(Embedder):
    """通过 OpenAI 兼容端点调用百炼的向量化模型。

    为什么用 openai 这个 SDK 而不是 dashscope SDK？
    因为百炼的 OpenAI 兼容端点已经标准化，openai SDK 更通用，
    而且以后换供应商（智谱/硅基流动）只要改 base_url。
    """

    def __init__(
        self,
        model: str = EMBEDDING_MODEL,
        dim: int = EMBEDDING_DIM,
        batch_size: int = EMBEDDING_BATCH,
        cache_path: str | None = None,
    ) -> None:
        # 延迟导入：离线模式完全不碰这个包，因此没装它也能跑通切分和检索。
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                f"没有安装 openai SDK，无法调用向量化接口：{exc}\n"
                f"    pip install openai\n"
                f"（只想跑离线链路的话不需要它，加 --offline）"
            ) from exc

        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self._client = OpenAI(api_key=require_api_key(), base_url=EMBEDDING_BASE_URL)

        # 缓存：文本 md5 -> 向量。重跑索引时能省掉绝大部分费用。
        self._cache_path = cache_path
        self._cache: dict[str, list[float]] = {}
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                self._cache = json.load(f)

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")

        result: list[np.ndarray | None] = [None] * len(texts)

        # 1) 先查缓存
        pending: list[tuple[int, str]] = []
        for i, text in enumerate(texts):
            key = hashlib.md5(text.encode("utf-8")).hexdigest()
            if key in self._cache:
                result[i] = np.asarray(self._cache[key], dtype="float32")
            else:
                pending.append((i, text))

        # 2) 剩下的按批请求
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start + self.batch_size]
            vectors = self._embed_batch_with_retry([t for _, t in batch])

            for (idx, text), vec in zip(batch, vectors):
                result[idx] = vec
                key = hashlib.md5(text.encode("utf-8")).hexdigest()
                self._cache[key] = vec.tolist()

            # 百炼对 embedding 有 QPS 限制，批次之间稍微停一下，
            # 比被限流后指数退避等待更划算（重试一次要等好几秒）
            if start + self.batch_size < len(pending):
                time.sleep(0.2)

        if self._cache_path:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f)

        # 这里绝不能写 np.vstack([v for v in result if v is not None])。
        # 那种写法在有条目失败时会**静默少一行**，向量和 chunk 从此错位一格，
        # 之后所有检索结果都是错的 —— 而且不报任何错。
        # 宁可在这里炸掉，也不要让一个错位的索引被建起来。
        missing = [i for i, v in enumerate(result) if v is None]
        if missing:
            raise RuntimeError(
                f"有 {len(missing)} 条文本没能向量化（下标 {missing[:5]}…）。"
                f"若强行继续，向量会与 chunk 错位且不报错，因此中止。"
            )

        matrix = np.vstack(result).astype("float32")  # type: ignore[arg-type]
        return _l2_normalize(matrix)

    def _embed_batch_with_retry(self, batch: list[str], max_retries: int = 3) -> list[np.ndarray]:
        from openai import APIConnectionError, APIStatusError, RateLimitError

        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = self._client.embeddings.create(
                    model=self.model,
                    input=batch,
                    dimensions=self.dim,
                )
                # 注意：返回顺序不保证和输入一致，必须按 index 排序。
                # 这是个经典的静默 bug —— 顺序错了向量和文本就错位了，
                # 检索结果会变得莫名其妙，但不会报任何错。
                items = sorted(resp.data, key=lambda d: d.index)
                return [np.asarray(d.embedding, dtype="float32") for d in items]
            except (RateLimitError, APIConnectionError) as exc:
                last_error = exc
                wait = 2 ** attempt
                print(f"  [embedding] 第 {attempt + 1} 次失败（{type(exc).__name__}），{wait}s 后重试")
                time.sleep(wait)
            except APIStatusError as exc:
                # 4xx 是我们自己的问题（key 错、模型名错、超长），重试没用
                raise RuntimeError(
                    f"embedding 请求被拒绝：{exc.status_code} {exc.message}\n"
                    f"检查：1) QWEN_API_KEY 是否是**按量计费**的 key "
                    f"2) 模型名 {self.model} 是否已开通"
                ) from exc

        raise RuntimeError(f"embedding 重试 {max_retries} 次仍失败") from last_error


# ---------------------------------------------------------------------------
# 离线实现：只为把链路跑通，没有语义能力
# ---------------------------------------------------------------------------


class OfflineHashEmbedder(Embedder):
    """确定性哈希伪向量。**不是真的 embedding，不要用它评测质量。**

    用途只有一个：在没网、或者不想花额度的时候，验证
    「切分 -> 建索引 -> 双路召回 -> 融合 -> 重排 -> 生成」这条链路
    的代码是通的。

    它的原理是把词哈希到固定维度上做词袋，所以它其实是个"戴着向量帽子
    的 BM25"—— 同义词、改写句一律匹配不上。
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        # 提示信息不用 emoji：这是个库类，构造它的可能是 MCP Server、
        # 批处理脚本、CI —— 那些环境不一定会先初始化控制台编码。
        # 在 GBK 控制台上 print 一个 emoji 会抛 UnicodeEncodeError，
        # 把"温和地降级到离线模式"变成"一次工具调用崩溃"。
        print("[warn] 离线模式：使用哈希伪向量，只有字面匹配能力，不可用于评测")

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        from bm25 import tokenize  # 复用同一套分词，保证行为一致

        matrix = np.zeros((len(texts), self.dim), dtype="float32")
        for row, text in enumerate(texts):
            for token in tokenize(text):
                h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
                matrix[row, h % self.dim] += 1.0
        return _l2_normalize(matrix)


def build_embedder(offline: bool = False, dim: int | None = None, **kwargs) -> Embedder:
    """工厂。上层代码只调这个，不直接 new 具体类。

    dim 要能传下来：加载已有索引时必须用**建库时的维度**，
    否则查询向量和库里的向量维度对不上。
    """
    if offline:
        return OfflineHashEmbedder(dim=dim or 256)

    cache = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "index_store", "embed_cache.json"
    )
    return DashScopeEmbedder(dim=dim or EMBEDDING_DIM, cache_path=cache, **kwargs)


if __name__ == "__main__":
    import sys

    from console import enable_utf8_output

    enable_utf8_output()

    text = "eth0 的 RTT 从 15ms 升到 210ms，同时 TCP 丢包率达到 3.2%"

    try:
        emb = build_embedder(offline="--offline" in sys.argv)
    except RuntimeError as exc:
        # 缺 SDK、缺 key 都是这里抛的，消息本身已经写清楚了，
        # 不要再套一层 traceback 把它埋掉。
        print(f"跳过：{exc}")
        sys.exit(1)

    vec = emb.embed_query(text)
    print(f"模型维度: {emb.dim}")
    print(f"向量 shape: {vec.shape}")
    print(f"L2 范数: {np.linalg.norm(vec):.4f}  （应为 1.0，说明归一化生效）")
