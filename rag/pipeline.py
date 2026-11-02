"""
端到端 RAG —— 检索 + 生成。

===========================================================================
这一层只做三件事，别的都不做
===========================================================================

  1. 把问题交给 HybridRetriever 拿回 top-k 资料
  2. 把资料和问题拼成 prompt
  3. 调 LLM 出答案

**不要在这里写 if-else 判断"用户问的是延迟还是丢包"。**
那是规则引擎的思路，等于把召回逻辑写死在代码里。
让检索器去决定哪些资料相关 —— 这也正是做 RAG 而不是做规则匹配的意义。

===========================================================================
Prompt 里最要紧的一条约束
===========================================================================

"资料里没有就说没有，不要用你的先验知识编。"

这一条不写，模型会表现得非常聪明：它会用训练时学到的网络知识
一本正经地分析，而你**根本分不清哪句来自你的数据、哪句来自它的记忆**。
对运维诊断这种场景，一个编造的 RTT 数值比一句"资料不足"危险得多。

写法上有个技巧：让它**引用 chunk_id**。模型一旦要写出"依据 log_ab12"，
它就很难凭空编 —— 编出来的 id 对不上，人一眼能看出来。
这叫可溯源性，也是 RAG 相比纯 prompt 的核心价值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import config
from bm25 import BM25Index
from chunking import Chunk
from embedder import build_embedder
from hybrid import Hit, HybridRetriever, format_hits
from llm import LLMError, QwenChat
from rerank import DashScopeReranker, NoOpReranker
from vector_store import VectorStore


SYSTEM_PROMPT = """你是一名资深网络运维工程师，负责基于监控数据诊断网络问题。

工作规则：
1. 只依据【参考资料】作答。资料里没有的信息，直接说"现有资料不足以判断"，
   绝不要用你的一般网络知识补充具体数值或结论。
2. 每个结论后面用括号标出依据的资料编号，例如（依据 log_ab12）。
   找不到依据的判断，宁可不写。
3. 区分【观测到的现象】和【推断的原因】。日志只能证明现象，
   原因需要知识库支撑；两者混在一起讲是诊断事故的常见来源。
4. 如果资料里有相互矛盾的数据（比如同一时间窗一块网卡正常另一块异常），
   明确指出矛盾，不要强行圆成一个结论。
5. 用中文回答，结构清晰，控制在 400 字以内。"""

USER_TEMPLATE = """【参考资料】
{context}

【问题】
{question}

请基于上述资料作答。若资料不足，请明确指出缺少什么信息。"""


@dataclass
class Answer:
    question: str
    answer: str
    hits: list[Hit] = field(default_factory=list)
    context: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "error": self.error,
            "sources": [
                {
                    "chunk_id": h.chunk.chunk_id,
                    "source": h.chunk.metadata.get("source"),
                    "interface": h.chunk.metadata.get("interface"),
                    "time_window": h.chunk.metadata.get("time_window"),
                    "topic": h.chunk.metadata.get("topic"),
                    "score": round(h.score, 4),
                    "retrieval": h.source,
                }
                for h in self.hits
            ],
        }


class NetworkRAG:
    """完整的检索增强诊断系统。"""

    def __init__(
        self,
        retriever: HybridRetriever,
        llm: QwenChat | None = None,
    ) -> None:
        self.retriever = retriever
        self._llm = llm

    @property
    def llm(self) -> QwenChat:
        """懒加载生成客户端。

        这样"只检索不生成"的路径（评测、调试召回）不需要装 anthropic SDK、
        也不需要 API Key —— 依赖只在真正用到时才产生。
        """
        if self._llm is None:
            self._llm = QwenChat()
        return self._llm

    # ------------------------------------------------------------------

    @classmethod
    def load(cls, offline: bool = False, use_rerank: bool = True) -> "NetworkRAG":
        """从磁盘索引加载。索引不存在时会给出可执行的提示。"""
        try:
            store = VectorStore.load(config.INDEX_DIR)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"{exc}\n先建索引：python build_index.py --sample"
            ) from exc

        embedder = build_embedder(offline=offline, dim=store.dim)

        if not use_rerank or offline:
            reranker = NoOpReranker("离线模式或未启用")
        else:
            reranker = DashScopeReranker()

        retriever = HybridRetriever(store, embedder, reranker)
        return cls(retriever)

    # ------------------------------------------------------------------

    def retrieve(self, question: str, top_k: int | None = None) -> list[Hit]:
        """只检索，不生成。调试召回质量时用这个，不烧生成的钱。"""
        return self.retriever.retrieve(question, top_k=top_k)

    def ask(self, question: str, top_k: int | None = None) -> Answer:
        """完整流程：检索 -> 拼 prompt -> 生成。"""
        hits = self.retrieve(question, top_k=top_k)
        context = format_hits(hits)

        if not hits:
            # 检索为空时**不要**硬着头皮调 LLM。
            # 没有资料还调，模型只能靠记忆编，正好踩中我们最想避免的坑。
            return Answer(
                question=question,
                answer="没有检索到相关资料，无法基于现有数据作答。",
                hits=[],
                context=context,
            )

        prompt = USER_TEMPLATE.format(context=context, question=question)

        try:
            text = self.llm.chat(prompt, system=SYSTEM_PROMPT)
        except LLMError as exc:
            # 生成挂了不等于整条链路挂了：检索结果本身就有价值，
            # 降级成"只回资料不回复结论"，比抛异常让上层崩掉好。
            return Answer(
                question=question,
                answer="（生成服务暂时不可用，以下为检索到的原始资料）\n\n" + context,
                hits=hits,
                context=context,
                error=str(exc),
            )

        return Answer(question=question, answer=text, hits=hits, context=context)

    # ------------------------------------------------------------------
    # 给 Agent 用的工具接口
    # ------------------------------------------------------------------

    def as_tool(self, query: str, top_k: int = 5) -> dict[str, Any]:
        """包装成 Function Calling 的 tool 返回值。

        这个形状是给 agent/tools.py 用的 —— 和 get_conn_stats 那些并列，
        让 ReAct 循环里的模型能自己决定"什么时候需要翻历史日志"。

        注意返回里**不含 answer**，只含检索到的资料。
        因为 Agent 场景下，模型自己就是那个"生成答案"的角色，
        再套一层生成会导致它对着自己的结论做二次总结，纯浪费 token。
        """
        hits = self.retrieve(query, top_k=top_k)
        return {
            "query": query,
            "count": len(hits),
            "results": [
                {
                    "chunk_id": h.chunk.chunk_id,
                    "text": h.text,
                    "interface": h.chunk.metadata.get("interface"),
                    "time_window": h.chunk.metadata.get("time_window"),
                    "topic": h.chunk.metadata.get("topic"),
                    "source": h.chunk.metadata.get("source"),
                }
                for h in hits
            ],
        }


# ---------------------------------------------------------------------------
# 对应的 tool schema —— 直接粘到 agent/tools.py 的 TOOLS 列表里
# ---------------------------------------------------------------------------

RETRIEVE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_network_history",
        "description": (
            "在历史网络监控日志和网络诊断知识库中做混合检索（向量+BM25+重排）。"
            "当需要回答『刚才/某个时间点网络怎么样』『历史上有没有类似故障』"
            "『某个指标的正常范围是多少、异常怎么排查』这类涉及历史数据或"
            "通用知识的问题时调用。返回按相关性排序的资料片段。"
            "注意：查当前实时指标请用 get_conn_stats，不要用这个工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "检索问题，用自然语言。带上关键限定词能显著提升准确率，"
                        "例如『eth0 在 10:02 附近的 RTT 和丢包情况』"
                        "就比『网络怎么样』好得多。"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回资料条数，默认 5，一般不用改。",
                },
            },
            "required": ["query"],
        },
    },
}


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()

    # 快速自检：只检索，不调生成
    rag = NetworkRAG.load(offline=config.OFFLINE_EMBEDDING, use_rerank=False)
    for q in ["eth0 延迟突然升高是什么时候", "WiFi 信号弱怎么排查"]:
        print("=" * 66)
        print(f"问题：{q}")
        for h in rag.retrieve(q, top_k=3):
            print(f"  {h.score:.4f} [{h.source}] {h.chunk.chunk_id}  "
                  f"{h.chunk.metadata.get('time_window') or h.chunk.metadata.get('topic')}")
        print()
