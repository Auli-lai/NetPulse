"""
长期记忆 —— 把诊断结论存下来，下次遇到类似问题能想起来。

===========================================================================
三层记忆，各自解决不同尺度的问题
===========================================================================

    ┌─────────────┬──────────────────────┬──────────────────────────────┐
    │ 层次         │ 存什么               │ 活多久                        │
    ├─────────────┼──────────────────────┼──────────────────────────────┤
    │ 短期（工作）  │ 完整工具调用轨迹      │ 一次诊断内                    │
    │             │ （agent.py 的消息列表）│ 超长就裁剪旧工具结果          │
    ├─────────────┼──────────────────────┼──────────────────────────────┤
    │ 会话         │ 问题 + 结论摘要       │ 一次会话内（多轮问答）        │
    │             │ （session.py）        │ 只留最近几轮                  │
    ├─────────────┼──────────────────────┼──────────────────────────────┤
    │ 长期         │ 关键结论的向量        │ 跨会话、跨进程                │
    │             │ （本文件）            │ 落盘，可召回                  │
    └─────────────┴──────────────────────┴──────────────────────────────┘

三层的区别不是"存多存少"，而是**什么该被记住**：

  · 短期记的是**过程** —— 调了什么工具、拿到了什么数据。
    它的价值在**当前这一次推理**里，诊断结束就没用了。
  · 会话记的是**上下文** —— 用户上一句问了什么。
    它的价值在**紧接着的下一句**里，用来消解"它"、"那个问题"这类指代。
  · 长期记的是**结论** —— 什么现象对应什么根因。
    它的价值在**下一次遇到同类问题**时。

面试时的说法：**过程、上下文、结论，三种记忆的粒度、生命周期、
召回方式都不同，不该用一套机制硬扛。**

===========================================================================
长期记忆和 RAG 的区别（这个必须分清楚，容易被问）
===========================================================================

两者都是"向量化 + 相似度召回"，但**索引的东西完全不同**：

    RAG       索引**外部知识** —— 日志原文、产品文档、知识库
              回答"世界上有什么"

    长期记忆   索引**自己的经历** —— 我上次遇到这个是怎么判断的
              回答"我以前见过吗"

一个是查资料，一个是回忆经验。混在一起会有个很隐蔽的后果：
**模型分不清哪句是"文档说的"、哪句是"我上次猜的"** ——
而上次的结论可能是错的。所以本项目的长期记忆单独存、单独标注来源。

===========================================================================
设计决策：JSONL 是事实源，FAISS 只是缓存
===========================================================================

和 RAG 那边不一样 —— 那边索引本身就是产物，重建一次很贵。
这边**记忆必须是可读、可改、可删的**：

  · 用户要能回答"你为什么记得这个？" —— 打开 memories.jsonl 就能看
  · 记错了要能手动删掉 —— 一行 JSON 删掉就完事
  · 索引坏了要能重建 —— 从 JSONL 重新向量化，数据不丢
  · 能进版本控制 —— 纯文本，diff 看得见

如果记忆只存在向量库里，上面四件事一件都做不到。
**"AI 的记忆"这种东西，人对它的信任来自能不能检查和纠正它。**

代价是每次改动都要重新向量化 —— 但记忆的增长是"一天几条"的量级，
不是日志那种一天几万条。这个代价可以忽略。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 路径：复用 rag/ 的向量化与向量库
# ---------------------------------------------------------------------------
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_AGENT_DIR)
_RAG_DIR = os.path.join(_PROJECT_ROOT, "rag")
if _RAG_DIR not in sys.path:
    sys.path.insert(0, _RAG_DIR)

from chunking import Chunk  # type: ignore  # noqa: E402
from embedder import Embedder, build_embedder  # type: ignore  # noqa: E402
from vector_store import VectorStore  # type: ignore  # noqa: E402

# 默认存储位置
DEFAULT_MEMORY_DIR = os.environ.get(
    "NETPULSE_MEMORY_DIR", os.path.join(_AGENT_DIR, "memory_store")
)

# 记录上限。超了按"最少被召回 + 最旧"淘汰。
DEFAULT_MAX_RECORDS = int(os.environ.get("NETPULSE_MEMORY_MAX", "500"))

# 从结论里抽"根因判断"那一段，用于生成摘要
_ROOT_CAUSE_RE = re.compile(r"#+\s*根因判断\s*\n+(.*?)(?=\n#|\Z)", re.S)
# 中文回答里常见的网卡名
_IFACE_RE = re.compile(r"\b(eth\d+|wlan\d+|enp\w+|wlp\w+|docker\d+|lo)\b")


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


@dataclass
class MemoryRecord:
    """一条长期记忆。

    注意 summary 和 conclusion 是**两个不同的字段**，这是有意的：

        conclusion  结论全文。给**模型**读的，要完整。
        summary     精简摘要。给**向量化**用的，要短、要有区分度。

    为什么不能直接拿结论全文去向量化：一次诊断的结论里
    "## 根因判断 ## 依据 ## 建议"这些标题是**每一条都一样的**，
    它们在向量里占了很大比重，会把真正有区分度的那几句稀释掉。
    结果是所有记忆的向量都长得很像，召回质量大幅下降。

    这是做检索时很典型的一类错误：**内容该存什么和该索引什么，是两件事。**
    """

    id: str
    question: str
    conclusion: str
    summary: str
    created_at: str
    interfaces: list[str] = field(default_factory=list)
    kind: str = "diagnosis"
    occurrences: int = 1
    recall_count: int = 0
    last_recalled_at: str | None = None
    tools_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def one_line(self) -> str:
        ts = self.created_at[:10]
        first = self.summary.strip().replace("\n", " ")[:70]
        extra = f" ×{self.occurrences}" if self.occurrences > 1 else ""
        return f"[{ts}] {first}{extra}"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _extract_summary(question: str, conclusion: str, limit: int = 260) -> str:
    """从结论里提炼用于向量化的摘要。

    优先取"根因判断"那一段 —— 那是最有信息量、也最有区分度的部分。
    抽不到（模型没按格式输出）就退回结论开头。
    """
    core = ""
    if m := _ROOT_CAUSE_RE.search(conclusion or ""):
        core = m.group(1).strip()
    if not core:
        core = (conclusion or "").strip()
    core = re.sub(r"[#*`>]", "", core)          # 去掉 markdown 标记
    core = re.sub(r"\s+", " ", core).strip()
    text = f"{question.strip()} {core}".strip()
    return text[:limit]


def _extract_interfaces(*texts: str) -> list[str]:
    found: list[str] = []
    for text in texts:
        for m in _IFACE_RE.finditer(text or ""):
            name = m.group(1)
            if name != "lo" and name not in found:
                found.append(name)
    return found[:5]


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------


class LongTermMemory:
    """诊断结论的持久化 + 向量召回。

    用法::

        mem = LongTermMemory()
        mem.add(question="网络很卡", conclusion="eth0 链路质量下降……")
        for rec, score in mem.recall("网速慢", top_k=3):
            print(score, rec.summary)
    """

    def __init__(
        self,
        directory: str = DEFAULT_MEMORY_DIR,
        embedder: Embedder | None = None,
        offline: bool | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> None:
        self.dir = directory
        self.records_path = os.path.join(directory, "memories.jsonl")
        self.index_dir = os.path.join(directory, "index")
        self.meta_path = os.path.join(directory, "index_meta.json")
        self.max_records = max_records

        self._records: list[MemoryRecord] = _load_jsonl(self.records_path)
        self._embedder = embedder
        self._offline = (
            os.environ.get("RAG_OFFLINE", "").lower() in ("1", "true", "yes")
            if offline is None
            else offline
        )
        self._store: VectorStore | None = None

    # ---------------- 依赖 ----------------

    @property
    def embedder(self) -> Embedder:
        """懒加载向量化器 —— 不用记忆功能就完全不碰网络/额度。"""
        if self._embedder is None:
            self._embedder = build_embedder(offline=self._offline)
        return self._embedder

    @property
    def store(self) -> VectorStore:
        """懒加载向量索引。索引不存在或过期就自动重建。"""
        if self._store is None:
            self._store = self._load_or_build_index()
        return self._store

    # ---------------- 写 ----------------

    def add(
        self,
        question: str,
        conclusion: str,
        *,
        interfaces: Sequence[str] | None = None,
        tools_used: Sequence[str] | None = None,
        kind: str = "diagnosis",
    ) -> MemoryRecord | None:
        """记一条。重复的（同样的问题 + 同样的结论）只增加计数，不重复存。

        返回写入的记录；如果内容为空或被判定为重复，返回 None 或已有记录。
        """
        conclusion = (conclusion or "").strip()
        question = (question or "").strip()
        if not question or not conclusion:
            return None

        # 去重键：问题 + 结论的归一化哈希。
        # 归一化（去空白）是必须的 —— 否则同一句话差一个换行就算两条。
        digest = hashlib.md5(
            re.sub(r"\s+", "", question + conclusion).encode("utf-8")
        ).hexdigest()

        existing = self._find_by_hash(digest)
        if existing is not None:
            existing.occurrences += 1
            self._persist()
            return existing

        record = MemoryRecord(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            question=question,
            conclusion=conclusion,
            summary=_extract_summary(question, conclusion),
            created_at=_now(),
            interfaces=list(interfaces) if interfaces else _extract_interfaces(question, conclusion),
            kind=kind,
            tools_used=list(tools_used or []),
        )
        record_hash = digest

        self._records.append(record)
        self._hashes = getattr(self, "_hashes", {})
        self._hashes[record_hash] = record.id

        self._evict_if_needed()
        self._persist()
        self._invalidate_index()
        return record

    def forget(self, record_id: str) -> bool:
        """删一条记忆。用户发现记错了时要能自己清掉。"""
        before = len(self._records)
        self._records = [r for r in self._records if r.id != record_id]
        if len(self._records) == before:
            return False
        self._persist()
        self._invalidate_index()
        return True

    def clear(self) -> int:
        n = len(self._records)
        self._records = []
        self._persist()
        self._invalidate_index()
        return n

    def _find_by_hash(self, digest: str) -> MemoryRecord | None:
        hashes = getattr(self, "_hashes", None)
        if hashes is None:
            self._hashes = hashes = {}
            for r in self._records:
                h = hashlib.md5(
                    re.sub(r"\s+", "", r.question + r.conclusion).encode("utf-8")
                ).hexdigest()
                hashes[h] = r.id
        rid = hashes.get(digest)
        if rid is None:
            return None
        return next((r for r in self._records if r.id == rid), None)

    def _evict_if_needed(self) -> None:
        """超上限时淘汰。优先扔掉"从没被召回过 + 最旧"的。

        为什么按"被召回次数"排而不是纯按时间：被反复召回的记忆说明它有用，
        不该因为新来一条就被挤掉。这是很朴素但很有效的价值信号 ——
        和缓存淘汰里的 LFU 是同一个思路。
        """
        if len(self._records) <= self.max_records:
            return
        self._records.sort(key=lambda r: (r.recall_count, r.created_at))
        overflow = len(self._records) - self.max_records
        self._records = self._records[overflow:]

    def _persist(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        with open(self.records_path, "w", encoding="utf-8") as f:
            for r in self._records:
                f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")

    # ---------------- 索引 ----------------

    def _fingerprint(self) -> str:
        """记录集的指纹。用来判断索引是不是过期了。"""
        h = hashlib.md5()
        for r in self._records:
            h.update(r.id.encode("utf-8"))
            h.update(r.summary.encode("utf-8"))
        return h.hexdigest()

    def _load_or_build_index(self) -> VectorStore:
        if not self._records:
            return VectorStore(dim=self.embedder.dim)

        expected = self._fingerprint()
        if os.path.exists(self.meta_path):
            try:
                with open(self.meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                if meta.get("fingerprint") == expected and meta.get("dim") == self.embedder.dim:
                    return VectorStore.load(self.index_dir)
            except (json.JSONDecodeError, FileNotFoundError, KeyError):
                pass  # 缓存坏了就重建，JSONL 才是事实源

        return self.rebuild_index()

    def rebuild_index(self) -> VectorStore:
        """从 JSONL 重建向量索引。

        这是"JSONL 是事实源"这个设计最直接的回报：
        索引怎么坏都不怕，数据一条不丢。
        """
        store = VectorStore(dim=self.embedder.dim)
        if self._records:
            chunks = [
                Chunk(chunk_id=r.id, text=r.summary, metadata={"kind": r.kind})
                for r in self._records
            ]
            store.add(chunks, self.embedder)
        store.save(self.index_dir)
        os.makedirs(self.dir, exist_ok=True)
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "fingerprint": self._fingerprint(),
                    "dim": self.embedder.dim,
                    "count": len(self._records),
                    "built_at": _now(),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        self._store = store
        return store

    def _invalidate_index(self) -> None:
        """记录变了，索引作废。下次用到时懒重建。"""
        self._store = None
        if os.path.exists(self.meta_path):
            try:
                os.remove(self.meta_path)
            except OSError:
                pass

    # ---------------- 读 ----------------

    def recall(self, query: str, top_k: int = 3, min_score: float = 0.0) -> list[tuple[MemoryRecord, float]]:
        """按相似度召回过往记忆。"""
        if not self._records or not (query or "").strip():
            return []

        try:
            vector = self.embedder.embed_query(query)
            hits = self.store.search(vector, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            # 记忆检索失败**绝不能**让诊断失败 —— 它只是个加分项。
            # 这是本模块最重要的一条容错：没有记忆的诊断仍然能跑。
            print(f"[warn] 长期记忆召回失败（不影响诊断）：{type(exc).__name__}: {exc}")
            return []

        by_id = {r.id: r for r in self._records}
        out: list[tuple[MemoryRecord, float]] = []
        for chunk, score in hits:
            if score < min_score:
                continue
            record = by_id.get(chunk.chunk_id)
            if record is not None:
                out.append((record, score))
        return out

    def mark_recalled(self, records: Iterable[MemoryRecord]) -> None:
        """记一次召回。用于淘汰时的"价值"判断（见 _evict_if_needed）。"""
        touched = False
        for r in records:
            r.recall_count += 1
            r.last_recalled_at = _now()
            touched = True
        if touched:
            self._persist()

    def all(self) -> list[MemoryRecord]:
        return list(self._records)

    def recent(self, n: int = 5) -> list[MemoryRecord]:
        return sorted(self._records, key=lambda r: r.created_at, reverse=True)[:n]

    def stats(self) -> dict[str, Any]:
        return {
            "count": len(self._records),
            "path": self.records_path,
            "index_built": os.path.exists(self.meta_path),
            "dim": self.embedder.dim if self._embedder is not None else None,
            "offline": self._offline,
            "max_records": self.max_records,
            "most_recalled": [
                r.one_line()
                for r in sorted(self._records, key=lambda r: r.recall_count, reverse=True)[:3]
                if r.recall_count
            ],
        }

    def __len__(self) -> int:
        return len(self._records)


def _load_jsonl(path: str) -> list[MemoryRecord]:
    """读 JSONL。**坏行跳过而不是整体失败** —— 记忆是可手改的文件，
    用户手抖改坏一行不该让整个记忆库打不开。"""
    if not os.path.exists(path):
        return []
    records: list[MemoryRecord] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(MemoryRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError) as exc:
                print(f"[warn] {path} 第 {lineno} 行坏了，已跳过：{exc}")
    return records


# ---------------------------------------------------------------------------
# 给提示词用的渲染
# ---------------------------------------------------------------------------


def render_recall(recalled: Sequence[tuple[MemoryRecord, float]], max_chars: int = 200) -> str:
    """把召回的记忆渲染成注入提示词的文本。

    注意每一条都标了日期和"这是过去的结论" —— 因为**过去的结论可能是错的**，
    必须让模型知道这不是事实、是经验，可以质疑。
    """
    if not recalled:
        return ""
    lines = ["【过往类似诊断（是你以前的判断，不是事实，可以参考但必要时质疑）】"]
    for record, _score in recalled:
        ts = record.created_at[:10]
        text = record.conclusion.strip().replace("\n", " ")
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        once = f"（出现过 {record.occurrences} 次）" if record.occurrences > 1 else ""
        lines.append(f"· [{ts}]{once} 问：{record.question.strip()[:60]}")
        lines.append(f"  当时的结论：{text}")
    return "\n".join(lines)


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()

    import tempfile

    os.environ.setdefault("RAG_OFFLINE", "1")

    # 用临时目录自检，不碰真实记忆
    with tempfile.TemporaryDirectory() as tmp:
        mem = LongTermMemory(directory=tmp, offline=True)
        print(f"记忆目录：{tmp}\n")

        mem.add(
            question="网络很卡，帮我看看",
            conclusion=(
                "## 根因判断\neth0 的 RTT 升到 210ms 且丢包率 3.2%，"
                "主动 Ping 与系统 RTT 同时偏高，判断为本地链路质量问题。\n"
                "## 依据\n· RTT 210ms\n## 建议\n1. 检查网线"
            ),
        )
        mem.add(
            question="WiFi 信号很弱",
            conclusion=(
                "## 根因判断\nwlan0 的 RSSI 降到 -88dBm，属于信号极差，"
                "判断为距离路由器过远或有遮挡。\n## 依据\n· RSSI -88dBm"
            ),
        )
        # 重复写入应该只增加计数
        mem.add(
            question="网络很卡，帮我看看",
            conclusion=(
                "## 根因判断\neth0 的 RTT 升到 210ms 且丢包率 3.2%，"
                "主动 Ping 与系统 RTT 同时偏高，判断为本地链路质量问题。\n"
                "## 依据\n· RTT 210ms\n## 建议\n1. 检查网线"
            ),
        )

        print(f"共 {len(mem)} 条记忆")
        for r in mem.all():
            print(f"  · {r.one_line()}")
            print(f"      summary: {r.summary[:80]}")
            print(f"      网卡: {r.interfaces}")
        print()

        recalled = mem.recall("eth0 延迟很高", top_k=2)
        print(f"查询「eth0 延迟很高」召回 {len(recalled)} 条：")
        for record, score in recalled:
            print(f"  {score:.3f}  {record.question}")
        print()
        mem.mark_recalled([r for r, _ in recalled])

        print("注入提示词的渲染结果：")
        print(render_recall(recalled, max_chars=60))
        print()
        print("统计：", json.dumps(mem.stats(), ensure_ascii=False, indent=2))
