"""
Rerank 重排 —— 混合检索之后的第二道筛子。

===========================================================================
为什么召回之后还要重排？
===========================================================================

因为**召回和精排是两个不同的任务，不该用同一个模型。**

    召回 (Recall)   从几百个 chunk 里捞出 20 个"可能相关"的
                    目标：宁可错杀，不可放过。要快。
    重排 (Rerank)   把这 20 个按"相关性"精细排序，取前 5 个
                    目标：准确。可以慢，因为候选只有 20 个。

向量检索为了能支撑百万级数据，被迫把 query 和 document **分开编码**
（双塔 / bi-encoder）—— 各自算好向量存起来，查询时只能比向量距离。
代价是 query 和 document 之间没有真正的交互，细粒度的相关性判断很粗糙。

Rerank 模型（cross-encoder）把 query 和 document **拼在一起**送进模型，
让它们做完整的注意力交互。精度高得多，但没法预计算，只能对少量候选做。

所以流程是：**便宜但粗的召回 -> 昂贵但准的重排**。

===========================================================================
百炼的 rerank 有两套端点，别搞混
===========================================================================

    gte-rerank-v2       /api/v1/services/rerank/text-rerank/text-rerank
                        （DashScope 原生格式，已于 2026-05-30 下线）
    qwen3-rerank        用同一个原生端点也行，或走 /compatible-api/v1/reranks

本模块用的是 **DashScope 原生端点 + 嵌套请求体**：

    {"model": "...", "input": {"query": ..., "documents": [...]},
     "parameters": {"top_n": 5, "return_documents": false}}

注意这和 embedding 那边的扁平 OpenAI 风格完全不同 ——
两套协议长得不一样是百炼的历史包袱，写的时候照抄文档别凭感觉。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

import requests

from config import RERANK_BASE_URL, RERANK_MODEL, require_api_key
from chunking import Chunk


@dataclass
class RankedItem:
    chunk: Chunk
    score: float


class Reranker(ABC):
    @abstractmethod
    def rerank(self, query: str, chunks: Sequence[Chunk], top_k: int) -> list[RankedItem]:
        ...


# ---------------------------------------------------------------------------
# 真实实现：qwen3-rerank
# ---------------------------------------------------------------------------


class DashScopeReranker(Reranker):
    """调用百炼的重排模型。

    注意 relevance_score 是**当前请求内的相对分数**（0~1），
    不同请求之间的分数不可比 —— 不要拿它设绝对阈值。
    """

    def __init__(
        self,
        model: str = RERANK_MODEL,
        instruct: str | None = None,
        max_retries: int = 3,
    ) -> None:
        self.model = model
        self.max_retries = max_retries
        # instruct 用来告诉模型"你要按什么标准排序"。
        # 官方建议用英文写，默认是问答检索。
        # 这里改成运维诊断的口径，让模型知道要按"这段日志能不能解释这个现象"排，
        # 而不是按"文字像不像"排。
        self.instruct = instruct or (
            "Given a network operations query, retrieve log segments and knowledge "
            "entries that explain the observed network symptom or help diagnose it."
        )

    def rerank(self, query: str, chunks: Sequence[Chunk], top_k: int) -> list[RankedItem]:
        chunks = list(chunks)
        if not chunks:
            return []
        top_k = min(top_k, len(chunks))

        payload = {
            "model": self.model,
            "input": {
                "query": query,
                "documents": [c.text for c in chunks],
            },
            "parameters": {
                "return_documents": False,
                "top_n": top_k,
                "instruct": self.instruct,
            },
        }
        headers = {
            "Authorization": f"Bearer {require_api_key()}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(
                    RERANK_BASE_URL, headers=headers, json=payload, timeout=30
                )

                # 4xx（429 除外）是请求本身有问题，重试一百次也一样。
                # 百炼的响应体里会写明原因，直接透出来 ——
                # 不然用户只能看到"400 Bad Request"，完全不知道要改什么。
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise RuntimeError(
                        f"rerank 请求被拒绝（HTTP {resp.status_code}）：\n"
                        f"{resp.text[:500]}\n"
                        f"排查方向：\n"
                        f"  1) 模型 '{self.model}' 在你的百炼账号里开通了吗？\n"
                        f"  2) QWEN_API_KEY 的类型和这个端点匹配吗？\n"
                        f"     （按量计费的 key 用 dashscope.aliyuncs.com）\n"
                        f"  3) 候选文档是不是太长/太多？qwen3-rerank 单文档上限 4000 token，\n"
                        f"     单请求上限 120000 token"
                    )

                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return self._parse(resp.json(), chunks)
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                last_error = exc
                # HTTPError 如果是 4xx（我们自己的请求有问题），重试没意义
                if isinstance(exc, requests.HTTPError) and not str(exc).startswith(
                    ("HTTP 429", "HTTP 5")
                ):
                    raise
                wait = 2 ** attempt
                print(f"  [rerank] 第 {attempt + 1} 次失败（{exc}），{wait}s 后重试")
                time.sleep(wait)

        raise RuntimeError(f"rerank 重试 {self.max_retries} 次仍失败") from last_error

    def _parse(self, data: dict, chunks: list[Chunk]) -> list[RankedItem]:
        # 原生端点的结果包在 output.results 里
        results = data.get("output", {}).get("results")
        if results is None:
            raise RuntimeError(f"rerank 返回结构不认识：{str(data)[:300]}")

        items: list[RankedItem] = []
        for r in results:
            idx = r.get("index")
            if idx is None or not (0 <= idx < len(chunks)):
                continue
            items.append(RankedItem(chunk=chunks[idx], score=float(r["relevance_score"])))
        return items


# ---------------------------------------------------------------------------
# 空实现：离线模式 / 未开通 rerank 时的降级
# ---------------------------------------------------------------------------


class NoOpReranker(Reranker):
    """不排序，原样截断。用于离线跑通链路或降级。

    有了它，rerank 服务挂掉时系统只是**变差**，而不是**崩掉** ——
    这叫优雅降级，线上系统的基本要求。
    """

    def __init__(self, reason: str = "未启用") -> None:
        self.reason = reason

    def rerank(self, query: str, chunks: Sequence[Chunk], top_k: int) -> list[RankedItem]:
        return [
            RankedItem(chunk=c, score=float(len(chunks) - i))
            for i, c in enumerate(list(chunks)[:top_k])
        ]


if __name__ == "__main__":
    from chunking import Chunk
    from console import enable_utf8_output

    enable_utf8_output()

    docs = [
        Chunk("a", "eth0 的 RTT 从 15ms 升到 210ms，同时 TCP 丢包率达到 3.2%"),
        Chunk("b", "wlan0 的 RSSI 从 -65dBm 降到 -88dBm，WiFi 信号劣化"),
        Chunk("c", "RTT 过高的常见原因是网络拥塞、路由抖动、DNS 解析延迟"),
    ]

    try:
        r = DashScopeReranker()
        for item in r.rerank("eth0 延迟突然变高是什么原因", docs, top_k=3):
            print(f"{item.score:.4f}  {item.chunk.chunk_id}  {item.chunk.text[:50]}")
    except Exception as exc:
        print(f"rerank 调用失败（可能未开通 qwen3-rerank）：{exc}")
