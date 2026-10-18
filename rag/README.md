# rag/ —— NetPulse 的检索层（混合检索 + Rerank）

把 eBPF 采集的历史日志和网络诊断知识库做成**可检索**的，
用「向量 + BM25 双路召回 → RRF 融合 → Rerank 精排」回答
"某个时间点网络怎么样""这个故障历史上出现过吗""这个指标异常该怎么排查"。

这一层是 Week 2 Day 4 的产出：**改造 eBPF 项目的检索部分**。

---

## 一、先回答你的问题：原来那套为什么跑不起来

`AI-assisted analysis/` 里有 8 个脚本
（`simple_rag_analyzer` / `optimized_network_rag` / `true_rag_analyzer` /
`vector_rag_analyzer` / `local_vector_rag_analyzer` / `interactive_rag` …），
你每次打开都不知道该跑哪个。问题不在你，在代码：

| 问题 | 具体表现 | 后果 |
|---|---|---|
| **API Key 硬编码在源码里** | 每个文件底部都有 `api_key = "YOUR_DASHSCOPE_API_KEY_HERE"` | 不改源码就跑不了；而且不能提交到 git |
| **8 个入口职责重叠** | simple / optimized / true / vector / local_vector 各写一遍同样的逻辑 | 不知道哪个是"最新版"，改 bug 要改 8 处 |
| **向量是假的** | `local_vector_rag_analyzer.py` 的 `SimpleEmbeddings` 是**哈希词袋**，不是 embedding | 它只有字面匹配能力，同义词一律匹配不上，但代码里叫它 "Embeddings" |
| **切分是盲切** | `RecursiveCharacterTextSplitter(chunk_size=800, overlap=150)` | 把同一次故障的因果链拦腰切断 |
| **没有评测** | 无从判断检索好不好 | 改任何参数都是盲调 |
| **没有混合检索** | 只有向量一路 | 搜 `eth0` 会召回 `wlan0` 的内容 |

本目录重写成一条链路、一个入口、Key 从环境变量读、有评测。

---

## 二、快速开始

```bash
# 0. 依赖（在 WSL / Ubuntu 上跑，这个项目本来就依赖 Linux）
cd rag
pip install -r requirements.txt

# 1. 配 Key（百炼控制台 -> API-KEY 管理 创建）
export QWEN_API_KEY='sk-你的key'

# 2. 建索引 —— 用自带样本日志，不需要起 eBPF 服务、不需要 root
python build_index.py --sample

# 3. 看效果：混合检索比单路强多少
python eval_recall.py

# 4. 问一个真实问题
python demo.py -q "10:02 前后 eth0 发生了什么"

# 5. 交互模式
python demo.py
```

**没有 Key 也想先跑通？** 全程加 `--offline`：

```bash
python build_index.py --sample --offline
python eval_recall.py --offline
python demo.py --offline -q "eth0 延迟升高"
```

离线模式用哈希伪向量替代真实 embedding，只验证「切分 → 建索引 →
双路召回 → 融合 → 重排」这条链路是通的。**它没有语义能力，
评测出来的分数不作数**，代码里也如实标注了。

---

## 三、链路

```
                        ┌──────────────────┐
   日志 / 知识库 ───────>│  结构感知切分     │  chunking.py
                        └────────┬─────────┘
                                 │
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
        ┌────────────────┐              ┌────────────────┐
        │ 向量索引 FAISS │              │  BM25 倒排索引 │  bm25.py
        │ vector_store.py│              │  jieba 分词    │
        └───────┬────────┘              └───────┬────────┘
                │ 稠密召回 20 条                 │ 稀疏召回 20 条
                └───────────────┬───────────────┘
                                ▼
                       ┌─────────────────┐
                       │  RRF 融合排序   │  hybrid.py
                       └────────┬────────┘
                                ▼
                       ┌─────────────────┐
                       │ Rerank 精排     │  rerank.py
                       │  qwen3-rerank   │
                       └────────┬────────┘
                                ▼ top 5
                       ┌─────────────────┐
                       │  Prompt 拼装    │  pipeline.py
                       └────────┬────────┘
                                ▼
                       ┌─────────────────┐
                       │ Qwen 生成       │  llm.py
                       │ Anthropic 协议  │
                       └─────────────────┘
```

---

## 四、文件职责

| 文件 | 干什么 | 关键点 |
|---|---|---|
| `config.py` | 所有端点、模型名、参数 | **一个 Key 打三个端点**，见下节 |
| `chunking.py` | 日志/知识库 → chunk | 按「网卡 + 时间窗」切，不按字符切 |
| `bm25.py` | 稀疏检索 | **自己实现 BM25**，含 k1/b 参数与 IDF |
| `embedder.py` | 向量化 | 百炼 `text-embedding-v3`，带缓存和重试 |
| `vector_store.py` | FAISS 索引 | `IndexFlatIP` + L2 归一化 = 余弦相似度 |
| `hybrid.py` | RRF 融合 + 统筹 | 只用排名不用分数，绕开量纲不可比 |
| `rerank.py` | 重排 | `qwen3-rerank`，带优雅降级 |
| `llm.py` | 生成 | Anthropic Messages 协议（和 OpenAI 有三处不同） |
| `knowledge.py` | 复用已有知识库 | 不复制一份，避免两边不同步 |
| `pipeline.py` | 端到端 + Agent tool 接口 | 含 `RETRIEVE_TOOL_SCHEMA` |
| `build_index.py` | 建索引 CLI | |
| `eval_recall.py` | **检索评测** | 这份最有价值，见第六节 |
| `demo.py` | 问答 CLI | 唯一入口 |
| `console.py` | 控制台编码 | Windows 管道输出会崩，见下节 |

---

## 四之二、Windows 上必须知道的一个坑：管道输出会崩

这个坑很值得记，因为它的表现形式特别有欺骗性：

```bash
python bm25.py                  # 正常，emoji 中文都好
python bm25.py | tail -5        # UnicodeEncodeError，直接崩
python demo.py > out.txt        # 同样崩
```

**同一个程序，加不加管道结果不同。** 原因是 stdout 指向哪里：

| stdout 指向 | Python 怎么编码 | 结果 |
|---|---|---|
| 终端（isatty） | 走 `_WindowsConsoleIO`，直接写 UTF-16，**编码器不参与** | 什么都正常 |
| 管道 / 重定向 | 用本地码页（简中 Windows = GBK/cp936），`errors='strict'` | ✅ ⚠️ ❌ 编不出来 → 抛异常 |

所以它的特征是：**你手工怎么测都是好的，一 `| tee`、一重定向、被别的程序
subprocess 调起来、或者进了 CI 立刻炸**，而且崩在 `print` 那一行，看起来
和业务逻辑毫无关系。你甚至会以为是检索代码写错了。

> 顺带说：中文 GBK 是有的，出问题的只有 emoji、制表符、数学符号。
> 但「变成问号」和「进程挂掉」是两回事 —— 后者会让管道下游拿到半截输出。

修法在 `console.py`，分两种情况，**只做一半不行**：

1. 输出到**管道/文件** → 直接换 UTF-8 就对了（没有终端在解释这些字节）
2. 输出到**真终端** → 光改 Python 这边不够，终端还在按 GBK 解释我们写出去
   的字节，中文会整片乱码。必须先用 `SetConsoleOutputCP(65001)` 把终端自己
   也切到 UTF-8
3. 第 2 步失败时**不能硬上 UTF-8**，退回原编码 + `errors='replace'`

每个可执行脚本的入口调用一次：

```python
if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()
    main()
```

放在 `__main__` 而不是模块顶层，是因为 import 别人的模块不该顺手改掉他的输出流。

> **面试里可以这么讲**：我遇到一个只在 CI 里复现的编码崩溃，本地怎么测都是好的。
> 定位下来是 stdout 指向终端和指向管道时 Python 走两套完全不同的编码路径 ——
> 终端走 `_WindowsConsoleIO` 直接写 UTF-16、编码器不参与，管道走本地码页 + strict。
> 顺带意识到光把 Python 改成 UTF-8 还不够，Windows 终端自己也有码页，
> 得两边一起切。这类「环境差异导致的行为不一致」比单纯的算法题更能体现排查能力。

---

## 五、一个 Key，三个端点（最容易卡住的地方）

你给的 `base_url` 是百炼的 **Anthropic 兼容端点**。有两个事实必须先说清楚：

**1. 它背后跑的是 Qwen，不是 Claude。**
所以 `model` 要填 `qwen3-max` 这类 Qwen 模型名，
填 `claude-opus-5` 一定失败。可选值以百炼控制台为准。

**2. Anthropic 协议里没有 embedding 接口。**
Anthropic 官方就不提供向量化模型，兼容端点自然也没得转发。所以：

| 用途 | 协议 | 端点 | 模型 |
|---|---|---|---|
| 生成 | Anthropic Messages | `/apps/anthropic` | `qwen3-max` |
| 向量化 | OpenAI 兼容 | `/compatible-mode/v1` | `text-embedding-v3` |
| 重排 | DashScope 原生 | `/api/v1/services/rerank/text-rerank/text-rerank` | `qwen3-rerank` |

三个 Key 是同一个，但请求体和响应格式**三种都不一样**。
这是百炼的历史包袱，照抄文档，别凭感觉写。

### 踩坑速查

- **`base_url` 不要写 `/v1` 结尾**。要停在 `/apps/anthropic`，
  多写一层会拼成 `/v1/v1/messages` 而 404。
- **这个端点不提供 `/v1/models`**，所以任何"先列模型再选一个"的
  客户端逻辑都会失败，必须显式指定模型名。
- **Key 类型和端点要匹配**：按量计费的 Key（`sk-` 开头）配
  `dashscope.aliyuncs.com`；Coding Plan 的 Key（`sk-sp-` 开头）
  要配 `coding.dashscope.aliyuncs.com`。混用会报错。
- **rerank 有两套格式**：`qwen3-rerank` 可以走原生端点
  （嵌套 `input`/`parameters`），也可以走 `/compatible-api/v1/reranks`
  （扁平）。本目录用原生那套。
- **`gte-rerank-v2` 已于 2026-05-30 下线**，别再用。

### Anthropic 协议 vs OpenAI 协议（三处差异）

```python
# OpenAI                         # Anthropic
messages=[                       system="你是...",          # ← 1. 顶层参数
  {"role":"system", ...},        messages=[{"role":"user",...}],
  {"role":"user", ...}
]                                max_tokens=2048,          # ← 2. 必填

content = resp.choices[0]        for b in resp.content:    # ← 3. 内容块列表
    .message.content               if b.type == "text":
                                     b.text
```

第 3 点最容易出错：`resp.content` 是**块列表**，可能是 `text` /
`thinking` / `tool_use`，直接当字符串用会拿到一个对象。

---

## 六、评测 —— 这次改造最值钱的部分

`eval_recall.py` 对比四种方案。下面是**离线模式**（38 chunk / 10 query，哈希伪向量）的实测结果：

```
方案                      Recall@5    Hit@5      MRR
仅 BM25（稀疏）             0.900      0.900      0.600
仅向量（稠密）              1.000      1.000      0.683
混合 · RRF 融合            1.000      1.000      0.708
混合 + Rerank             1.000      1.000      0.708
```

**"我做了混合检索"是一句话术；"混合检索把 MRR 从 X 提到 Y"是证据。**
面试官听到前者会问"你怎么知道有效"，听到后者会问"你怎么评测的"——
而后者是你已经准备好的问题。

### ⚠️ 但这组数字只能证明"链路通了"，不能证明"混合检索有用"

必须自己把这条说清楚，否则就是自欺：

- `OfflineHashEmbedder` 本质是**袋装词哈希**，也就是"戴了向量帽子的 BM25"。
  两条路径在它眼里高度重合，所以 Recall 那两行根本拉不开差距
  （稠密 1.000、混合 1.000 —— 混合的收益是 0）。
- 真正拉开差距的是 **MRR 0.600 → 0.708**，也就是**排序质量**：
  融合让正确答案更靠前。这是 RRF 的真实贡献。
- 换成真的 `text-embedding-v3` 之后，「WiFi 信号弱 vs RSSI 下降」这类
  同义改写的 query 才会体现出稠密的优势，那时 Recall 的差距才会出现。
  **拿到 API Key 后第一件事就是重跑这张表，用新数字替换掉它。**

把这句话留在 README 里而不是删掉，是因为面试里主动说出"我这组实验的
局限在哪"远比报一个漂亮数字可信 —— 漂亮数字面试官一追问就塌。

评测集用**谓词标注**而不是写死 chunk_id：

```python
EvalCase("wlan0 的信号强度一路下降", _log("wlan0"), "网卡名是硬区分，BM25 占优")
```

好处是换日志、重切分、换 embedding，评测集都不用改。
局限也写清楚：谓词对"相关性"的定义比人工标注粗。
**能主动说出方法的局限，比声称方法完美更能体现工程判断力。**

脚本还会打印**逐条对比表**，显示每条 query 分别被哪一路召回——
这张表比总分有用，它告诉你"在什么情况下有用"，才能指导优化。

⚠️ 样本量提醒：几十个 chunk + 十来条 query，数字有指示性但没有统计显著性。

---

## 七、核心概念（面试要点）

### 为什么切分不能按字符数

日志里"RTT 从 15ms 升到 210ms"和"丢包率涨到 3.2%"是**同一次故障的两个侧面**。
按 800 字符盲切，边界正好落在中间，检索时只能召回半条证据，
LLM 拿到"RTT 升高了"但不知道同时还丢了包 —— **诊断结论直接错**。

正确做法是以「一块网卡 + 一个时间窗」为界，每个 chunk 自洽。

### 为什么需要混合检索

```
日志里全是 eth0 / 3.2% / 210ms / POOR 这种强字面的词。

向量       语义空间对这类 token 不敏感
           搜「eth0 丢包」，它可能把「wlan0 丢包」排前面 —— 在它看来几乎一样

BM25       只看词频和逆文档频率
           「eth0」和「wlan0」是完全不同的词
```

两者互补。**向量管语义，BM25 管字面。**

### 为什么融合用 RRF 而不是加权求和

加权求和 `α·向量分 + (1-α)·BM25分` 有个致命问题：**两路分数不在一个量纲上**。

- 向量余弦相似度：`[0,1]`，集中在 0.6~0.9，差 0.05 就很明显
- BM25 分数：`[0,+∞)`，常见 2~15，换语料整体尺度就变

硬加权的话 α 要跟着语料反复调，换数据就失效。

RRF 绕开了这个问题 —— **它只用排名，不用分数**：

```
RRF(d) = Σ_r  1 / (k + rank_r(d))        k = 60

k 的作用：让「第1名」和「第3名」的差距不至于过于悬殊
  有 k：1/61 = 0.0164  vs  1/63 = 0.0159   差 3%
  无 k：1/1  = 1.0     vs  1/3  = 0.33     差 3 倍（第一名一票否决）
```

一个文档被两路同时召回时分数叠加，这正是我们要的"两路都认为相关"。
**几乎不需要调参**，这是它至今是工业界默认方案的原因。

### 为什么召回之后还要 Rerank

**召回和精排是两个不同的任务，不该用同一个模型。**

| | 召回 (Recall) | 重排 (Rerank) |
|---|---|---|
| 任务 | 从几百个里捞出 20 个"可能相关" | 把 20 个精排，取前 5 |
| 目标 | 宁可错杀不可放过 | 准确 |
| 模型 | bi-encoder（双塔） | cross-encoder |
| 为什么 | query / doc **分开编码**，能预计算，快 | query 和 doc **拼在一起**，完整注意力交互，准 |
| 代价 | 细粒度相关性判断粗糙 | 无法预计算，慢 |

所以流程是：**便宜但粗的召回 → 昂贵但准的重排**。

### 为什么用 IndexFlatIP 而不是 IVF / HNSW

FAISS 的索引分两类：精确索引（Flat）暴力比对，结果 100% 准；
近似索引（IVF/HNSW）先聚类，快但会漏。

近似索引的存在意义是"千万级向量毫秒返回"。你现在几百个 chunk，
Flat 是微秒级，上 IVF 只会白白损失召回率还要调 nprobe。

**能说清"我为什么不用更高级的索引"，比"我用了最先进的索引"更能说明你懂取舍。**

---

## 八、接到 Agent 里

`pipeline.py` 里的 `NetworkRAG.as_tool()` 已经包装成 Function Calling 的形状，
直接加到 `agent/tools.py`：

```python
from pipeline import NetworkRAG, RETRIEVE_TOOL_SCHEMA

TOOLS.append(RETRIEVE_TOOL_SCHEMA)          # 加进工具列表

_rag = NetworkRAG.load()                    # 懒加载

def _search_network_history(query, top_k=5):
    return _rag.as_tool(query, top_k=top_k)
```

注意 `as_tool()` **只返回检索到的资料，不返回生成的答案**。
因为 Agent 场景下模型自己就是"生成答案"的角色，
再套一层生成会让它对着自己的结论做二次总结，纯浪费 token。

这样 Agent 就有了：**实时指标**（`get_conn_stats`，走 D-Bus）+ **历史检索**
（`search_network_history`，走 RAG）。这是两条不同的路径，
模型自己决定什么时候用哪条。

---

## 九、这一层怎么讲在简历上

> 为 eBPF 网络监控系统设计并实现混合检索层：基于日志结构（网卡 × 时间窗）
> 做语义单元切分，构建 FAISS 稠密索引与自研 BM25 稀疏索引双路召回，
> 以 RRF 融合后接 qwen3-rerank 精排；建立谓词标注评测集，
> 对比单路/混合/重排三种方案的 Recall@5 与 MRR 验证改造收益。

能展开讲的点（按面试价值排序）：

1. **为什么切分按语义单元而不是字符数** —— 讲因果链被切断的具体例子
2. **RRF 为什么比加权求和高明** —— 讲量纲不可比
3. **召回和重排为什么用不同模型** —— bi-encoder vs cross-encoder
4. **怎么在没有标注数据时做评测** —— 谓词标注，以及它的局限
5. **一个 Key 三个协议的工程细节** —— 说明你真的调通过，不是抄的
6. **哨兵值翻译** —— `-1000` 不翻译会被模型理解成"信号极差"

---

## 十、已知缺口（别在简历上写没做的）

- **日志来源是文件，不是实时流。** 现在是 `build_index.py` 离线灌，
  没有增量索引。日志文件很大时要改成定时增量重建。
- **BM25 索引在内存里，每次启动重建。** 几百个 chunk 没问题，
  上万就要落盘（或者上 Elasticsearch）。
- **评测集只有 10 条 query，统计上不够。** 这是指示性数字，
  不是结论。日志攒多了要扩到 50 条以上。
- **没有做上下文压缩。** `format_hits` 只是按 600 字符截断，
  更好的做法是按 token 预算动态分配。
- **rerank 未开通时会静默降级成不排序。** 会在输出里说明，但不会报警。

---

## 十一、和 `agent/` 的关系

```
agent/      LLM 的"手脚"—— 调 D-Bus 拿实时指标、执行 Ping       (Week 1)
rag/        LLM 的"记忆"—— 在历史日志和知识库里翻找              (Week 2)
AI-assisted analysis/   旧代码，保留作参考，不再维护
```

两个目录配合起来才是完整的"会自己排查的运维 Agent"：
一个负责**现在怎么样**，一个负责**历史上怎么回事、该怎么修**。
