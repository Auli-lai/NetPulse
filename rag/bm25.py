"""
BM25 稀疏检索 —— 自己实现，不依赖 rank_bm25。

===========================================================================
为什么要有它？纯向量检索在你这批数据上会漏。
===========================================================================

日志里全是 `eth0`、`wlan0`、`3.2%`、`210ms`、`POOR` 这种**强字面**的词。
向量的语义空间对这类 token 不敏感 —— 你搜「eth0 丢包」，向量可能把
「wlan0 丢包」也排前面，因为在它看来这两句几乎一样。

BM25 恰好相反：它只看词频和逆文档频率，「eth0」和「wlan0」是完全不同的词。

两者互补，所以叫「混合检索」：向量管语义，BM25 管字面。

===========================================================================
BM25 公式（面试会被问，记住这四行）
===========================================================================

    对每个查询词 q，文档 d 的得分：

        IDF(q) = ln( 1 + (N - df(q) + 0.5) / (df(q) + 0.5) )

        score(q, d) = IDF(q) * ( tf * (k1 + 1) )
                              / ( tf + k1 * (1 - b + b * |d| / avgdl) )

        N      文档总数
        df(q)  包含词 q 的文档数 —— 越稀有，IDF 越大
        tf     词 q 在文档 d 里出现的次数
        |d|    文档 d 的长度（词数）
        avgdl  所有文档的平均长度

    两个超参：
        k1 = 1.5   词频饱和系数。某词出现 20 次不该比出现 10 次重要一倍，
                   这个除法的分母让它增长逐渐饱和。
        b  = 0.75  文档长度归一化强度。长文档天然更容易命中词，
                   b 把长文档的分数压一压。b=0 完全归一化，b=1 完全归一化。

"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, Sequence

import jieba

# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------

# 日志里的技术词必须**整块保留**，否则 jieba 会把它们切碎。
# 实测 jieba 对多数技术词的切法是好的（eth0 / 210ms / 3.2% / RSSI 都能整块保留），
# 但这两类一定会切坏：
#   "223.5.5.5" -> "223.5", ".", "5.5"        IP 地址碎成三瓣
#   "10:00:30"  -> "10", ":", "00", ":", "30" 时间戳碎成五瓣
# 所以先把这些模式挖出来换成占位符，分词完再换回去。
_TOKEN_PATTERNS = [
    r"\d{1,3}(?:\.\d{1,3}){3}",          # IPv4: 223.5.5.5
    # 时间：同时认 "10:00" 和 "10:00:30"。
    # 必须能匹配两种粒度 —— 见 tokenize() 里"时间戳降级索引"的说明。
    r"\d{1,2}:\d{2}(?::\d{2})?",
    r"[a-zA-Z]+[a-zA-Z0-9_]*\d+",        # eth0 / wlan0 / tcp1 / qwen3
    r"\d+(?:\.\d+)?(?:ms|pps|MB/s|Mbps|dBm|%)",  # 210ms / 3.2% / 1200pps
    r"\d+\.\d+",                          # 浮点: 85.5
]

# 占位符必须是**纯 ASCII 字母数字**。
#
# 这里踩过一个坑，记下来：最初用的是 "\x00{index}\x00"（NUL 字符），
# 因为它在正常文本里绝不会出现，看起来是最安全的哨兵。
# 结果 jieba 把 NUL 当分隔符直接切掉，占位符碎成 ['\x00','0','\x00']，
# 还原时查表落空 —— 所有技术词全部丢失，句子只剩 ['0','rtt','1','升','2']。
#
# 最阴险的是它**不报错**：索引照样建起来，检索照样能跑，
# 只是字面匹配能力整个废掉，召回率悄悄变低。这种 bug 靠读代码看不出来，
# 只有把分词结果打出来看才会发现 —— 或者像下面 demo() 里那样写成断言。
_PLACEHOLDER = "zqzqzq{}zqzqzq"

# 五个模式合成一个正则，**必须单次扫描**。
#
# 第二坑：原来是 for 循环逐个 re.sub，结果后面的模式会回过头去匹配
# 前面刚插进去的占位符 —— "zqzqzq2" 恰好符合
# `[a-zA-Z]+[a-zA-Z0-9_]*\d+`（字母开头、数字结尾），于是被二次替换，
# 时间戳的占位符在查表时找不到，变成了裸的占位符字符串。
#
# 合成一个 alternation 之后，re.sub 从左到右扫一遍原串，
# 替换出来的内容不会再被自己扫到。顺序即优先级：IP 在浮点前面，
# 否则 "223.5.5.5" 会先被浮点规则咬掉一块。
_TECH_TOKEN_RE = re.compile("|".join(_TOKEN_PATTERNS))

# 停用词：中文里的虚词对 BM25 只有噪声，去掉能显著提升准确率
_STOPWORDS = set(
    "的 了 在 是 和 与 及 或 有 为 对 从 到 被 把 就 都 也 还 而 但 这 那 "
    "一个 一种 我们 你们 他们 它 他 她 我 你 会 能 要 可以 应该 可能 "
    "什么 怎么 如何 哪些 哪个 时候 情况 问题 上面 下面 里面".split()
)


def tokenize(text: str) -> list[str]:
    """把一段文本切成 BM25 用的词元。

    jieba 负责中文分词，正则占位负责保住技术词。
    """
    placeholders: dict[str, str] = {}

    def _stash(match: re.Match) -> str:
        token = match.group(0)
        key = _PLACEHOLDER.format(len(placeholders))
        placeholders[key] = token
        return key

    # 1) 技术词挖出来
    masked = _TECH_TOKEN_RE.sub(_stash, text)

    # 2) 中文分词
    tokens: list[str] = []
    for piece in jieba.cut(masked):
        piece = piece.strip()
        if not piece:
            continue
        # 3) 占位符还原
        if piece in placeholders:
            token = placeholders[piece].lower()
            tokens.append(token)

            # 时间戳降级索引（第三个坑，也是实测出来的）：
            #
            # 文档里写的是 "10:00:30"，但用户提问只会说"10:00 的时候怎么样"。
            # 两者的 token 天然对不上 —— 实测时 "10:00 的时候 eth0 还是正常的"
            # 这条 query 在四种检索方案下**全部漏召回**，就是栽在这里。
            #
            # 修法：文档侧的 "HH:MM:SS" 额外发出一个 "HH:MM"。
            # 反过来不行（查询侧少一个冒号就补不出秒），所以必须在文档侧做。
            #
            # 更一般的说法：**查询和文档的时间粒度不对称。**
            # 凡是"用户描述得比数据粗"的字段都有这个问题，都要在文档侧补一条粗粒度索引。
            if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", token):
                tokens.append(token[:5])
            continue
        if piece in _STOPWORDS:
            continue
        # 纯标点丢弃
        if not re.search(r"[\w一-鿿]", piece):
            continue
        tokens.append(piece.lower())

    return tokens


# ---------------------------------------------------------------------------
# BM25 索引
# ---------------------------------------------------------------------------


class BM25Index:
    """一份内存里的 BM25 倒排索引。

    用法::

        idx = BM25Index(doc_ids, doc_texts)
        idx.search("eth0 丢包", top_k=20)   # -> [('chunk_3', 4.82), ...]
    """

    K1 = 1.5
    B = 0.75

    def __init__(self, doc_ids: Sequence[str], doc_texts: Sequence[str]) -> None:
        if len(doc_ids) != len(doc_texts):
            raise ValueError("doc_ids 和 doc_texts 长度必须一致")

        self.doc_ids = list(doc_ids)
        self.n_docs = len(self.doc_ids)

        # 每篇文档的词频表
        self._doc_tf: list[Counter[str]] = []
        # 词 -> 出现在哪些文档里（倒排表）。只存文档下标，省内存。
        self._inverted: dict[str, set[int]] = {}
        self._doc_len: list[int] = []

        for i, text in enumerate(doc_texts):
            tokens = tokenize(text)
            tf = Counter(tokens)
            self._doc_tf.append(tf)
            self._doc_len.append(len(tokens))
            for term in tf:
                self._inverted.setdefault(term, set()).add(i)

        self._avgdl = (sum(self._doc_len) / self.n_docs) if self.n_docs else 0.0

        # IDF 只跟 df 有关，跟查询无关 —— 建索引时一次算好，查询时直接查表。
        # 这是 BM25 能比"每次全量扫描"快得多的原因。
        self._idf: dict[str, float] = {}
        for term, doc_set in self._inverted.items():
            df = len(doc_set)
            self._idf[term] = math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """返回 [(doc_id, score), ...]，按分数降序。分数为 0 的不返回。"""
        query_terms = tokenize(query)
        if not query_terms:
            return []

        scores: dict[int, float] = {}

        for term in query_terms:
            doc_set = self._inverted.get(term)
            if not doc_set:
                # 语料里从没出现过这个词 -> IDF 无意义，跳过。
                # 注意这里不要用"平滑后的极小 IDF"，那会让生僻词乱打分。
                continue
            idf = self._idf[term]

            # 只遍历包含该词的文档，而不是遍历全部文档 —— 倒排表的意义所在
            for doc_idx in doc_set:
                tf = self._doc_tf[doc_idx][term]
                dl = self._doc_len[doc_idx]
                denom = tf + self.K1 * (1 - self.B + self.B * dl / self._avgdl)
                scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * tf * (self.K1 + 1) / denom

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [(self.doc_ids[i], s) for i, s in ranked]

    def __len__(self) -> int:
        return self.n_docs


def demo() -> None:
    """自检：确认技术词没被分词器切碎。

    跑：python bm25.py

    这些断言不是摆设。上面 _PLACEHOLDER 的注释里记的那个 bug
    （技术词全丢、且不报错）就是靠这里发现的 ——
    把期望的 token 写死成断言，改分词逻辑时它会立刻叫出来。
    """
    cases = [
        ("eth0 RTT 从 15ms 升到 210ms", ["eth0", "rtt", "15ms", "210ms"]),
        ("wlan0 RSSI -65dBm 网络质量 good", ["wlan0", "rssi", "65dbm", "网络", "质量", "good"]),
        ("10:00:30 TCP 丢包率 3.2% 目标 223.5.5.5", ["10:00:30", "tcp", "3.2%", "223.5.5.5"]),
        # 文档侧的 "10:00:30" 必须同时产出粗粒度的 "10:00"，
        # 否则查询里说"10:00"的用户永远匹配不上（见 tokenize 里的注释）
        ("10:00:30 发生了什么", ["10:00:30", "10:00"]),
        # 查询侧的粗粒度时间自己也得是完整 token，不能被 jieba 切成 ['10','00']
        ("10:00 的时候 eth0", ["10:00", "eth0"]),
    ]

    failed = 0
    for text, expected in cases:
        tokens = tokenize(text)
        missing = [e for e in expected if e not in tokens]
        status = "OK  " if not missing else "FAIL"
        print(f"[{status}] {text}")
        print(f"        -> {tokens}")
        if missing:
            print(f"       ❌ 丢失 token: {missing}")
            failed += 1
        print()

    if failed:
        print(f"❌ {failed}/{len(cases)} 条用例失败：分词器正在吞掉技术词。")
        print("   技术词丢失不会报错，只会让 BM25 静默失效，必须修。")
        raise SystemExit(1)
    print("✅ 技术词全部保留\n")

    idx = BM25Index(
        ["c1", "c2", "c3"],
        [
            "eth0 RTT 从 15ms 升到 210ms 网络质量变差",
            "wlan0 RSSI 从 -65dBm 降到 -88dBm 信号弱",
            "eth0 TCP 丢包率 3.2% 严重",
        ],
    )
    print("查询「eth0 丢包」 ->", idx.search("eth0 丢包", top_k=3))
    print("查询「wlan0 信号」->", idx.search("wlan0 信号", top_k=3))


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()
    demo()
