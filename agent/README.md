# agent/ —— NetPulse 的 Agent 层（手写 ReAct 循环 + Function Calling）

把 eBPF 采集、D-Bus 接口、RAG 检索统一包装成模型可调用的工具，
做成"会自己动手排查"的运维 Agent。

**这是整个项目最核心的产出物** —— 简历里那句"基于 ReAct 范式实现 Agent 循环，
模型自主编排多步工具链"对应的就是这个目录。

---

## 一、30 秒跑起来（不需要 Key，不需要装任何东西）

```bash
cd "AI-Network-Monitoring-System/agent"
python main.py --dry-run
```

这条命令用**假模型 + 假工具**跑一遍完整推理链，把
"模型思考 → 调工具 → 结果回灌 → 再思考 → 出结论"整条过程打出来。

**第一次接触一定先跑这个。** 不用配 Key、不用装 dbus、不用起 C++ 服务，
就能看清楚循环长什么样。先看清楚循环，再去接真实依赖，比反过来快得多。

```
❯ 网络很卡，帮我看看是什么问题

[第 1 轮] 用户说网络卡。卡可能是延迟高、丢包，也可能是带宽被占满了。
         先调 get_network_health 拿结论和问题清单，再决定往哪个方向查。
  🔧 get_network_health  (0ms)
[第 2 轮] 健康分 64 分（一般），问题清单里有『RTT 偏高』和『TCP 丢包率 3.2%』两项。
         现在需要具体数值来判断严重程度，调 get_conn_stats。
  🔧 get_conn_stats  (0ms)
[第 3 轮] RTT 210ms、丢包 3.2%、带宽 2.7MB/s、活跃连接 23 个。带宽不算饱和，
         所以不像单纯拥塞。接下来要定位是哪条连接在传数据，调 get_flows 看连接明细。
  🔧 get_flows  (0ms)
[第 4 轮] 最活跃的连接是到 223.5.5.5 的。关键一步：为了区分是本地链路问题
         还是目标站点问题，主动 Ping 一个已知稳定的公网地址。
  🔧 ping_host  (0ms)
[第 5 轮] 主动 Ping 205ms，和系统上报的 RTT 210ms 基本一致 —— 两个独立来源
         都指向本地链路。现在查历史日志。
  🔧 search_network_history  (0ms)
```

---

## 二、目录结构

```
agent/
├── README.md              本文件
├── main.py                ★ 命令行入口（你要用这个）
├── agent.py               ★ ReAct 循环 + 容错 + 轨迹（核心产出物）
├── llm_client.py          Anthropic Messages 协议 + Function Calling
├── tools.py               ★ 7 个工具的定义与执行（唯一事实源）
├── memory.py              长期记忆（结论向量化存储与召回）
├── session.py             会话记忆（多轮上下文）
├── netpulse_client.py     D-Bus 客户端（含后加的 GetFlows）
├── offline_demo.py        --dry-run 用的假模型剧本 + 假工具
├── measure.py             量"平均调用几个工具"（简历上那个数要用它量）
├── test_agent.py          ★ 循环容错测试（全离线，144 项断言）
├── test_tools.py          D-Bus 接线冒烟测试（需要 server 在跑）
├── _verify_getflows.py    GetFlows 的 C++ 改动离线验证
└── requirements.txt
```

数据流：

```
        ┌──────────────── 用户的自然语言问题 ────────────────┐
        ▼                                                    │
   agent.py  ──►  llm_client.py  ──►  Qwen (百炼 Anthropic 端点)
   ReAct 循环       协议翻译              │
        │                                │ 要求调工具
        │   ◄────────────────────────────┘
        ▼
   tools.py  ──►  netpulse_client.py ──► D-Bus ──► C++/eBPF（实时指标）
             └─►  ../rag/             ──► FAISS  ──► 历史日志 + 知识库
```

---

## 三、三种用法

### ① 离线演示（先从这个开始）

```bash
python main.py --dry-run              # 完整推理轨迹
python main.py --dry-run --json       # 轨迹以 JSON 输出（Week 4 前端要用）
```

### ② 单次提问（真实诊断）

```bash
export QWEN_API_KEY='sk-你的百炼key'    # Windows: $env:QWEN_API_KEY='sk-...'
python main.py -q "网络很卡，帮我看看"
```

### ③ 交互模式

```bash
python main.py
```

### 其它

```bash
python main.py --list-tools           # 看模型能调哪些工具、schema 长什么样
python main.py --max-rounds 5 -q "..."  # 限制推理轮次
python main.py --quiet -q "..."       # 只输出结论，不打印推理过程
```

---

## 四、跑真实模式前的检查清单

四条缺任何一条都**不会让程序崩** —— 对应的工具会返回 error，模型看得到并会绕开。
这是有意设计的降级，不是 bug。但缺得太多，诊断质量就没法保证。

| # | 依赖 | 缺了会怎样 | 怎么补 |
|---|---|---|---|
| 1 | `QWEN_API_KEY` | 模型调不动，直接没有结论 | `export QWEN_API_KEY='sk-...'` |
| 2 | RAG 索引 | `search_network_history` 报错 | `cd ../rag && python build_index.py --sample` |
| 3 | C++ 服务在跑 | 所有实时指标工具报错 | `sudo -E ./server/bin/weaknet-dbus-server` |
| 4 | `python3-dbus` | 同上（且是 Linux 专有） | `sudo apt install python3-dbus` |

程序启动时会自动检查前三条并给出提示（`main.py` 的 `_preflight_warnings`）。

### ⚠️ 最容易踩的坑：`sudo` 会丢 D-Bus 地址

server 读 eBPF 需要 root，但**客户端和服务端必须挂在同一个 session bus 上**。
`sudo` 默认不保留 `DBUS_SESSION_BUS_ADDRESS`，于是 server 连到 root 自己的总线，
你的 Python 连用户自己的总线，**两边互相看不见**，报 `ServiceUnknown`。

```bash
sudo -E ./server/bin/weaknet-dbus-server
# 或
sudo DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS ./server/bin/weaknet-dbus-server
```

验证服务真的注册上了：

```bash
dbus-send --session --print-reply --dest=com.example.WeakNet \
    /com/example/WeakNet com.example.WeakNet.GetInterfaces
```

---

## 五、Agent 循环是怎么工作的

### 核心只有 30 行

```python
while True:
    turn = model.chat(messages, tools)          # 1. 问模型
    if not turn.tool_calls:                     # 2. 不要工具了 = 出结论
        return turn.text
    messages.append(assistant_message(turn))    # 3. 把模型的调用意图记下来
    for call in turn.tool_calls:
        result = runner.run(call.name, call.arguments)   # 4. 执行
        messages.append(tool_message(call, result))      # 5. 结果回灌
```

**这段代码能跑通演示，但上不了生产。** 因为它假设了：

- 模型给的参数永远合法 → 现实中它会漏必填项、传错类型、参数名拼错
- 工具永远调用成功 → 现实中服务会挂、网卡名会拼错
- 模型不会原地打转 → 现实中它会反复调同一个工具
- 上下文永远不会超长 → 现实中日志 JSON 几个回合就撑爆
- 模型总会停下来 → 现实中它能转到你账户没钱

这五种情况里任何一种发生，上面那个骨架要么崩掉、要么悄悄烧钱。
**`agent.py` 里剩下 300 行全是处理这五件事** —— 这也正是简历上
"设计了工具调用失败回灌、重复调用检测、轮次上限等容错机制"对应的真实工作量。

### 五种容错各自做了什么

| 失控 | 处理 | 在哪 |
|---|---|---|
| **参数非法** | 不执行，把错误 + 正确的参数列表回灌 | `_validate_arguments` |
| **工具失败** | `{"error": ...}` 回灌，标 `is_error=True`，循环继续 | `ToolRunner.run` |
| **重复调用** | 第 3 次相同调用硬拦截，不执行 | `_execute` 的指纹计数 |
| **轮次失控** | 达到上限后**禁用工具**再问一次，逼出结论 | `_force_conclusion` |
| **上下文超长** | 把旧工具结果**换内容**（不删消息） | `_trim_context` |

几个值得单独说的设计点：

**① 一次把参数问题说全，不要只报第一个。**
模型最常犯的错是参数名拼错（把 `hostname` 写成 `host`）。这时同时存在
"缺必填项"和"多未定义项"两个问题。只报第一个的话，模型补上 `hostname`
再调一次 —— 又因为 `host` 未定义失败，**白白多花一轮**。

**② 重复调用是"分级处理"而不是"一刀切"。**
第 1 次正常执行；第 2 次仍然执行（状态确实可能变了），但附带一句提醒；
第 3 次才硬拦截。一上来就拦会误伤"轮询确认"这种合理行为。

**③ 指纹必须排序。**
`{"top_n": 5, "protocol": "TCP"}` 和 `{"protocol": "TCP", "top_n": 5}`
是同一个调用。不排序的话重复检测直接失效 —— 见 `_fingerprint`。

**④ 强制收尾时不给工具。**
达到轮次上限时，直接返回"达到上限"是最省事的做法，但用户要的是**结论**不是错误码。
所以再问模型一次，让它用已攒下的真实数据做总结 —— 但 `tools=[]`，
这样它在协议层面就无法再发起调用，不可能再绕一圈。

**⑤ 裁剪上下文只能换内容，不能删消息。**
这是整个循环里**唯一会静默损坏**的状态。assistant 的每个 `tool_use`
都必须在紧随的 user 消息里有对应的 `tool_result`，少一个请求直接 400。
本地短对话测不出来（没触发裁剪），跑长了才突然开始报错，
而报错信息指向"消息格式错误"，完全看不出跟上下文管理有关。

所以 `_trim_context` 只把旧工具结果的**内容**替换成占位符，
消息本身、配对关系、`tool_call_id` 全部保留。`llm_client.check_tool_pairing()`
就是检查这条不变量的，测试里拿它做断言。

---

## 六、七个工具

| 工具 | 干什么 | 数据来源 |
|---|---|---|
| `get_conn_stats` | 网卡实时指标（连接数/RTT/丢包/信号/带宽/健康分） | D-Bus `HealthCheck` |
| `list_interfaces` | 列出网卡名 | D-Bus `GetInterfaces` |
| `ping_host` | 主动 Ping 指定目标 | D-Bus `Ping` |
| `get_network_health` | 健康分 + 问题清单（只给结论） | D-Bus `HealthCheck` |
| `get_flows` | **连接明细**（五元组 + 速率 + PID） | D-Bus `GetFlows` ← 新加的 |
| `search_network_history` | 历史日志 + 知识库混合检索 | `../rag/`（FAISS + BM25 + Rerank） |

### 为什么是 7 个，而不是简历上写的 8 个

这不是"少做了两个"，而是**工具设计原则**的结果。

简历上列的是 8 项**能力**：连接明细、丢包率、网卡状态、异步 Ping、
WiFi 信号强度、日志检索、抓包、健康分评估。

但其中三项（丢包率、WiFi 信号强度、健康分）**来自同一次 HealthCheck**。
如果按"每个指标一个 tool"拆开，每次诊断要调 4 次 D-Bus、烧 4 倍 token，
拿到的还是同一份 JSON。所以合并成了 `get_conn_stats`。

> **原则：一个 tool 对应一个「动作」，不是一个「数据切片」。**
> 这是工具设计里最容易被问到的点，也是"工具越多越好"这个直觉的反例。

所以对应关系是：

| 简历上的能力 | 现状 |
|---|---|
| 连接明细查询 | ✅ `get_flows`（本轮新加的 D-Bus 方法与工具） |
| 丢包率 / WiFi 信号强度 / 健康分评估 | ✅ 合并进 `get_conn_stats` / `get_network_health` |
| 网卡状态 | ✅ `list_interfaces` |
| 异步 Ping | ✅ `ping_host` |
| 日志向量检索 | ✅ `search_network_history` |
| **抓包** | ❌ **没做** —— 需要新写 C++ 抓包模块，工作量另算 |

**建议**：把简历上"8 个可调用工具"改成"7 个工具覆盖 8 项采集能力"。
这样说既准确，又能顺势讲上面那条工具设计原则 —— 比报一个虚数强得多。

### description 是工具设计里最重要的东西

模型只能靠 `description` 决定调不调、调哪个。所以每个工具的 description
都必须回答三个问题：

1. **能干什么** —— 这个大多数人都写
2. **什么时候调** —— 触发的场景，必须写
3. **和相似工具怎么区分** —— 最容易漏，也最容易翻车

第 3 点举本项目的例子：`get_conn_stats` 和 `get_network_health` 数据同源，
不写清楚模型会随便挑一个。所以 description 里明写了
"要原始指标用这个；只要结论和问题清单用那个"。

这条规则写成了测试：`test_agent.py` 断言所有工具的 description
都包含「什么时候调」四个字 —— 防止以后加工具时忘了写。

---

## 六之二、三层记忆

这是 Week 3 Day 7 的产出。**三层不是"存多存少"的区别，而是什么该被记住：**

| 层 | 存什么 | 活多久 | 代码 | 解决的问题 |
|---|---|---|---|---|
| 短期 | 完整工具调用轨迹 | 一次诊断内，超长就裁剪 | `agent.py` `_trim_context` | 这次推理要看得见过程 |
| 会话 | 问题 + 结论摘要 | 一次会话内，只留最近 3 轮 | `session.py` | "那 wlan0 呢"里的"那"指什么 |
| 长期 | 关键结论的向量 | 跨会话，落盘 | `memory.py` | "我以前见过这个吗" |

```
        ┌── 长期（memory.py）──┐   跨会话。索引的是**自己的结论**
        │  什么现象 → 什么根因  │
        └───────────┬──────────┘
                    │ 诊断开始时召回 top-3，注入提示词
        ┌───────────▼──────────┐
        │  会话（session.py）   │   一次会话内。只记问题+结论，不记过程
        └───────────┬──────────┘
                    │ 每轮注入"之前聊过什么"
        ┌───────────▼──────────┐
        │  短期（agent.py）     │   一次诊断内。完整轨迹，超长裁剪
        └──────────────────────┘
```

### 长期记忆和 RAG 的区别（这个必须分清，容易被问）

两者都是"向量化 + 相似度召回"，但**索引的东西完全不同**：

- **RAG**（`rag/`）索引**外部知识** —— 日志原文、知识库。回答"世界上有什么"
- **长期记忆**（`memory.py`）索引**自己的经历** —— 我上次是怎么判断的。回答"我以前见过吗"

混在一起的后果很隐蔽：**模型分不清哪句是"文档说的"、哪句是"我上次猜的"**，
而上次的结论可能是错的。
所以本项目分开存，而且 `recall_past_diagnoses` 的返回里明确写了
"这是你过去的判断，不是事实，冲突时以实时数据为准"。

### 两个踩过的坑

**① 占位文本被当成结论存进去了**

第一版用 `if diagnosis.answer:` 判断"有没有结论"。但模型没给出结论时，
`run()` 会把 answer 填成 `"（模型没有给出结论）"` —— **它是非空字符串**，
于是被当成结论写进长期记忆。之后每次召回都会捞出一条纯噪声。

修法不是在存之前做字符串匹配（改一次文案就失效），而是在 `Diagnosis` 上
显式加一个 `answer_is_model_output` 标记。
**"非空"和"有意义"是两回事。**

**② 测试污染了真实记忆库**

`test_agent.py` 第一版忘了关记忆，跑一次测试就往 `agent/memory_store/`
塞了 7 条假诊断。之后所有召回都被污染，而且假诊断里有真实的网卡名和数值，
**看起来还挺像真的，很难发现**。

修法：测试统一走 `make_agent()` 这个包装，默认 `use_memory=False`；
要测记忆行为的用临时目录。**测试必须是封闭的。**

### 为什么 JSONL 是事实源，FAISS 只是缓存

和 `rag/` 那边不一样 —— 那边索引本身就是产物。这边**记忆必须是可读、可改、可删的**：

- 用户要能回答"你为什么记得这个？" → 打开 `memories.jsonl` 就能看
- 记错了要能删 → `python main.py --forget mem_xxx`
- 索引坏了要能重建 → 从 JSONL 重新向量化，一条不丢

代价是每次改动都要重新向量化，但记忆是"一天几条"的量级，可以忽略。
**"AI 的记忆"这种东西，人对它的信任来自能不能检查和纠正它。**

---

## 七、GetFlows：本轮新加的 C++ 改动

### 为什么加

eBPF 那边（`flow_rate.bpf.c` 的 `current_sec` LRU_HASHMAP）一直在采五元组，
但那份数据此前**只用于在用户态算聚合值**（带宽、包速率、活跃连接数），
**没有出口**。结果是：Agent 能知道"eth0 有问题"，但没法知道"是哪条连接造成的" ——
**从现象到根因的那一步是断的。**

这个改动就是把那个出口打开。

### 改了哪几处

| 文件 | 改动 |
|---|---|
| `server/include/common.hpp` | 加 `kMethodGetFlows` 常量 |
| `server/include/dbus_service.hpp` | 加 `handleGetFlows` 声明 |
| `server/src/dbus_service.cpp` | 加分发分支 + `handleGetFlows` 实现（约 64 行） |
| `server/include/net_traffic.h` | 加 `boundInterface()` getter |

**不需要改 Makefile** —— 没有新增 .cpp 文件，只改了已有的。

### 重新编译并验证

```bash
make                          # 项目根目录
sudo -E ./server/bin/weaknet-dbus-server

# 验证
dbus-send --session --print-reply --dest=com.example.WeakNet \
    /com/example/WeakNet com.example.WeakNet.GetFlows
# 应该返回一个 JSON 字符串

cd agent && python3 test_tools.py    # 里面有一节 GetFlows 专项检查
```

### 两个必须知道的代价

1. **会阻塞约 1 秒。** 底层的 `sampleTopFlows` 要采两次快照求差才能算出速率，
   所以必须真的等一个采样窗口。
2. **只包含窗口内有过数据传输的连接。** 底层会丢掉增量为 0 的条目，
   所以条数通常少于 `HealthCheck` 里的 `active_flows`（那个是已建立的连接数）。
   **这不是 bug**，工具返回里的 note 字段也向模型说明了这一点。

### ⚠️ 这部分代码我没有编译验证过

本机是 Windows，装不了 dbus-1 和 libbpf，**没法真正编译服务端**。

我能做的验证已经做了 —— `_verify_getflows.py` 把 `handleGetFlows` 的
**函数原文**抽出来，配一组功能性的桩（假 D-Bus、假 NetTrafficAnalyzer），
用 g++ 编译并运行，再检查它吐出的 JSON：

```bash
python _verify_getflows.py
```

```
① 语法检查（g++ -fsyntax-only）…  ✅ 通过
② 运行并检查输出 JSON…            ✅ 全部 18 项通过
③ 异常路径：采集抛异常时…          ✅ 回的是合法 JSON 且带 error
```

这能挡住语法、括号、类型、JSON 拼写错误。**但挡不住真实 dbus API 签名
可能存在的差异。** 如果 `make` 报错，先看报错是不是指向 `net_traffic.h`
或 `dbus_service.cpp`，是的话把错误贴给我。

---

## 八、怎么测的（这部分是面试重点）

两个测试文件，**分工是刻意的**：

```bash
python test_agent.py     # 测循环：容错、重复检测、轮次上限、上下文裁剪
                         # 全离线：假模型 + 假工具，144 项断言

python test_tools.py     # 测接线：D-Bus 通不通、每个工具真实返回什么
                         # 需要 server 在跑
```

分开是因为**失败原因完全不同**：前者错了是逻辑问题，后者错了是环境问题。
混在一个文件里，报错时你得先判断是哪一类。

### 为什么必须用假模型

真模型是**不确定的** —— 同一句话跑两次，它可能一次调 3 个工具、一次调 5 个。
所以"工具报错后模型会不会自我修正"这种事，用真模型**根本没法写成断言**，
你区分不了"代码对了"和"这次模型碰巧表现好"。

把模型换成按剧本返回的 `ScriptedChatModel`，五种边界就都成了确定性、
可重复的断言：

```python
model = ScriptedChatModel([
    think("先查一下", call("get_conn_stats")),   # 第 1 次：正常调
    think("再查一下", call("get_conn_stats")),   # 第 2 次：执行 + 提醒
    think("还查一下", call("get_conn_stats")),   # 第 3 次：硬拦截
    final("就这样吧"),
])
result = NetworkAgent(model=model, runner=FakeRunner()).run("网络卡")

assert len(runner.calls) == 2          # 第 3 次没真正执行
assert result.steps[2].executions[0].intervention is not None
```

`NetworkAgent` 的 `model` 和 `runner` 都是**可注入的** —— 这个设计不是为了好看，
是为了能测。

> **面试可以这么讲**："Agent 循环最容易错的不是调模型，是那些边界。
> 而真模型是不确定的，没法用它写断言。所以我把模型抽象成接口，
> 测试里注入一个按剧本返回的假模型，那五类边界就都能写成确定性断言了。
> 这样改循环逻辑时我敢动，因为一跑就知道有没有踩坏。"

---

## 九、核心概念速查（面试）

### ReAct 是什么

> Reasoning + Acting。模型输出"思考"和"行动"交替进行，行动的结果再回灌给它。
> **本质就是多轮 Function Calling**，不是新东西。论文是 2022 年的，
> 核心代码 150 行 —— 这是这个方向的真实情况。

### Agent 和 Workflow 的区别

| | Workflow | Agent |
|---|---|---|
| 控制流 | 你写死在代码里 | 模型自己决定 |
| 适合 | 步骤固定的任务 | 步骤不确定、要探索的任务 |
| 调试 | 容易 | 难（不确定 + 多轮） |

**本项目的取舍**：诊断路径是"模型自己决定"（Agent），但
"每个工具怎么执行"是写死的（Workflow）。混用才是常态。

### 上下文管理

- **短期记忆**：对话历史。本项目的做法是超长时裁剪旧工具结果（见第五节第 ⑤ 点）
- **长期记忆**：跨会话的知识。本项目是 `rag/` 那套向量检索
- **关键约束**：裁剪不能破坏 `tool_use` / `tool_result` 配对

### Token 成本控制

```python
python main.py --dry-run --json | python -c "import json,sys; print(json.load(sys.stdin)['usage'])"
```

真实跑（`--json` 输出里有 `usage`）。几个能说的点：

- 工具结果里**别塞原始 JSON**，塞模型真正需要的字段（`get_flows` 只回 top N）
- **一个工具一次拿全**，而不是拆成四个工具调四次（见第六节）
- 历史裁剪（第五节第 ⑤ 点）
- 轮次上限既是稳定性手段，也是成本手段

---

## 十、接出去

### 作为 tool 给别的 Agent 用

`agent.py` 的 `NetworkAgent` 可以直接当子 Agent 用；
`tools.py` 的 `TOOLS` 是协议无关的，换协议只是换渲染：

```python
runner = ToolRunner()
runner.anthropic_tools()   # -> Anthropic 形状
runner.openai_tools()      # -> OpenAI 形状（百炼兼容模式也能用）
```

### MCP（Week 3 Day 5-6 的产出）

`tools.py` 里的 `Tool` 结构（name / description / JSON Schema / 执行）
**就是 MCP tool 的形状**。写 MCP Server 时：

```python
for tool in TOOLS:
    server.tool(name=tool.name, description=tool.description,
                schema=tool.parameters)(wrap(tool))
```

需要新写的只有"把 `ToolRunner.run` 的结果包成 MCP 的返回格式"，
工具定义和实现可以直接复用。

### SSE 流式（Week 4 的产出）

`NetworkAgent(on_event=...)` 已经把手写好了。事件类型和前端要显示的
一一对应，`main.py` 的 `Printer` 就是一个把事件打到终端里的实现：

| 事件 | 什么时候发 | 带什么 |
|---|---|---|
| `start` | 开始诊断 | `question` |
| `thought` | 模型每一轮的思考 | `round`, `text` |
| `tool_result` | 每个工具执行完 | `name`, `is_error`, `elapsed_ms`, `intervention` |
| `forcing_conclusion` | 达到上限强制收尾 | — |
| `final` | 最终结论 | `text` |
| `finish` | 结束 | `stopped_because`, `rounds` |

**把 `Printer` 里的 `print` 换成 `queue.put` 就是 SSE。**
`Diagnosis.to_dict()` 也已经保证可 `json.dumps`。

---

## 十一、已知缺口（别在简历上写没做的）

### ① 抓包工具：没做

需要新写 C++ 抓包模块（libpcap 或 eBPF），工作量远超本轮范围。
**简历上请删掉这一项**，或者明确标成"规划中"。

### ② 简历上这句话与代码不符，必须改

> 通过 LRU_HASHMAP 支持 6 万+ 并发连接的五元组、RTT、重传次数采集

实际情况（已逐行核对过源码）：

- `max_entries = 65536`，"6 万+"这个数字**是对的**
- 但 `flow_data` 里只有 `bytes` / `packets` / `pid` 三个字段，
  **五元组是 key，RTT 和重传次数根本不在这张表里**
- RTT 来自**另一条链路**：`rtt_monitor` 对 `223.5.5.5` 做 ICMP 探测，是**按网卡**的
- 重传次数来自 `net_tcp.cpp` 解析 netlink `tcp_info`，也是**按网卡聚合**的

**改成**："通过 LRU_HASHMAP 支持 6 万+ 并发连接的五元组与流量采集；
RTT 与重传次数按网卡维度从 ICMP 探测和 netlink 统计中获取"。

这不是小事 —— 面试官只要问一句"重传次数是按连接还是按网卡统计的"，
原话就对不上了。改完之后反而更好讲：你能说清**为什么**当初这么设计
（eBPF 侧采集成本 vs 精度）。

### ③ "单次诊断平均调用 4.2 个工具"：这个数字得自己跑出来

我没有真实跑过（需要 Key + 起服务 + 真实数据）。

**自己跑 20 次真实诊断，把真实均值填进去。** 方法是
`python main.py --json -q "..."` 的输出里有 `tool_calls` 字段。

真实数字哪怕比 4.2 小，也更可信 —— 被追问"这数怎么来的"时，
能说"我跑了 20 次统计的"和"我估的"是两个层次。

### ④ `--dry-run` 的假数据是编的

`offline_demo.py` 里所有数值都是为了演示编出来的，
**只能验证链路通不通，不能用来判断网络好坏，更不能写进简历。**

---

## 十二、和 `rag/` 的关系

```
agent/    LLM 的"手脚" —— 调 D-Bus 拿实时指标、执行 Ping、查连接明细
rag/      LLM 的"记忆" —— 在历史日志和知识库里翻找
```

两个目录配合起来才是完整的"会自己排查的运维 Agent"：
一个负责**现在怎么样**，一个负责**历史上怎么回事、该怎么修**。

`agent/tools.py` 里的 `search_network_history` 就是这两个世界的接缝 ——
它对模型来说是"一个工具"，对你来说是"隔壁那套 FAISS + BM25 + Rerank"。
