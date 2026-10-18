# mcp_server/ —— NetPulse 诊断工具的 MCP Server

把本项目的 6 个诊断工具、6 个知识资源、3 个诊断提示词，
通过 **MCP 协议（Model Context Protocol）** 暴露给任何支持 MCP 的客户端
（Claude Code / Claude Desktop / Cursor / 自研 Agent）。

**这是手册里 Week 3 Day 5-6 的产出物。**

---

## 一、30 秒跑起来

```bash
cd "AI-Network-Monitoring-System/mcp_server"

python server.py --list         # 看它暴露了什么（不起服务）
python server.py --demo         # 用假数据起 stdio 服务（不需要 C++ 服务）
python test_e2e_stdio.py        # 起真子进程 + 真客户端，完整验证
```

`--demo` 是个关键设计：**先用假数据把客户端配通，再去接真实服务。**

没有它的话，第一次配 Claude Desktop 会遇到"连上了但每个工具都报错"，
而你分不清是 MCP 配置错了还是 C++ 服务没起 —— 这是两个完全不同的排查方向。

---

## 二、MCP 是什么，解决什么问题

### 先说它不是什么

它不是新模型、不是新框架、不是"更聪明的 Agent"。
**它是一个工具接入标准**，一句话就能说完：

> 在 MCP 之前，每接一个 LLM 应用就要把工具重写一遍。

```
Claude Desktop 一套        ┐
自研 Agent 一套            ├─  同一批工具，写 N 遍，每遍格式还不一样
Cursor / VSCode 插件 一套  ┘
```

N 个模型 × M 个工具 = **N×M 份适配代码**。

MCP 把它降成 **N+M**：工具方实现一次 Server，模型方实现一次 Client，
中间走一套标准协议（JSON-RPC 2.0）。

> **面试一句话**：MCP 是工具接入的 USB-C。
> 在它之前每个设备一种接口，在它之后一种接口接所有设备。

### 和 Function Calling 的区别（常被混为一谈）

| | Function Calling | MCP |
|---|---|---|
| 是什么 | 模型的**能力**（输出结构化调用意图） | 工具的**接入协议** |
| 谁定义工具 | 你的应用代码 | 独立的 Server 进程 |
| 跨应用复用 | 不能，每个应用重写 | 能，一次实现处处可用 |
| 传输 | 无（就是一次 API 调用） | JSON-RPC 2.0（stdio / HTTP） |

**它们是互补的，不是竞争的。**
MCP 客户端拿到工具列表之后，**还是通过 Function Calling 让模型决定调哪个**。

所以本项目的层次是：

```
模型  ──Function Calling──►  Agent 循环（agent/agent.py）
                              │
                              ├─► 本地直接调工具（agent/tools.py）
                              └─► 或走 MCP 协议调远程工具（mcp_server/）
```

两条路用的是**同一份工具定义** —— 这是本目录最重要的设计，见第五节。

---

## 三、三类原语（最该先搞清的事）

MCP 有三类原语。**它们的区别不是学术分类，而是「谁控制」：**

| 原语 | 谁决定用不用 | 典型用途 | 本项目对应 |
|---|---|---|---|
| **Tools** | **模型**决定 | 有动作、有副作用的操作 | 6 个诊断工具 |
| **Resources** | **应用/用户**决定 | 只读的上下文数据 | 知识库、日志、实时快照 |
| **Prompts** | **用户**主动触发 | 预设提示词模板 | 3 个诊断模板 |

### 判断口诀

> **模型该自己决定什么时候做的 → tool**
> **人该决定给不给模型看的 → resource**

### 本项目的划分，以及为什么

**实时指标做成 tool**，因为"现在要不要看一眼指标"是**模型**该判断的事 ——
它得根据用户说的话决定查不查。做成 resource 的话就变成用户必须先手动加载，
那就不叫"会自己排查的 Agent"了。

**知识库做成 resource**，因为它是静态参考资料，**不该让模型自己决定**要不要加载。
那是应用层或用户的事（用户在客户端里勾选）。而且知识库不需要"查询"这个动作 ——
它就是一堆现成的文档，直接读就行。

**诊断模板做成 prompt**，因为它是**用户**点一下"用这个模板诊断"才触发的。
模型自己不该决定"我要用哪个提示词模板"。

### ⚠️ 一个容易搞错的点

`search_network_history` 是 **tool**，不是 resource —— 虽然它也是"查资料"。
区别在于：**它需要一个查询动作**（做混合检索），而不是把一份固定文档交出去。
知识库是"把这份文档给你"，检索是"帮我找找相关的"。

---

## 四、目录结构

```
mcp_server/
├── README.md            本文件
├── server.py            ★ MCP Server（工具/资源/提示三类原语）
├── bridge.py            ★ 从 agent/tools.py 桥接工具（单一事实源）
├── test_server.py       进程内测试（快，69 项）
├── test_e2e_stdio.py    端到端测试（真子进程 + 真客户端，37 项）
├── mcp.json.example     客户端配置模板
└── requirements.txt
```

---

## 五、核心设计：单一事实源

### 问题

MCP SDK 会**从函数的类型标注自动生成 JSON Schema**：

```python
@mcp.tool()
def ping_host(hostname: str) -> dict:
    ...
```

所以最直观的写法，是在 MCP Server 里把这 6 个工具重新声明一遍。

**但那样工具定义就有两份了，而且必然分叉：**

- `agent/tools.py` 里的 description 改了 → MCP 这边还是旧的
- MCP 这边加了个参数 → Agent 那边没有

分叉的后果特别隐蔽：**同一个模型，走 Agent 和走 MCP 表现不一样。**
你在 Agent 上测得好好的，接到 Claude Desktop 上发现它老不调某个工具 ——
因为两边给模型看的 description 已经不是同一份了。

而 description 恰恰是这个项目里**最贵的东西**（它决定了模型什么时候调、
和相似工具怎么区分）。两份必然分叉，分叉必然调错。

### 解法

单向桥接：`agent/tools.py` 是唯一事实源，MCP 只是它的另一种渲染。

```
agent/tools.py 的 Tool 对象（name + description + JSON Schema）
        │
        │  bridge.py
        ▼
按 JSON Schema 合成一个函数签名 ──► MCP SDK 读它生成协议 schema
```

具体做法（`bridge.py`）：

1. 按 JSON Schema 造一个 `inspect.Signature`
2. 造一个真的会调 `ToolRunner` 的函数
3. 把 `__signature__` / `__annotations__` 挂上去

于是 SDK 读到的签名和 schema 完全一致，而函数体是我们的分发器。

这有点"魔法"，所以 `bridge.py` 里每步都写了注释 —— 这类代码不解释清楚，
半年后自己都看不懂。

### 有测试守着

`test_server.py` 会逐字比对 MCP 里的 description 和 `agent/tools.py` 里的，
**不一致就失败**。所以这个设计不是靠自觉维持的。

> **面试可以这么讲**："我遇到的真实问题是同一个工具在两条链路上给模型看的
> description 不一致，导致同一个模型表现不一样，而且很难查。所以我把工具定义
> 抽成单一事实源，MCP 层只做渲染，再写个测试盯着两边别分叉。"

---

## 六、接到了什么

### Tools（6 个，从 `agent/tools.py` 桥接）

`get_conn_stats` / `list_interfaces` / `ping_host` /
`get_network_health` / `get_flows` / `search_network_history`

### Resources（6 个）

| URI | 内容 | 依赖 |
|---|---|---|
| `netpulse://knowledge` | 知识库目录（7 大类） | 无 |
| `netpulse://knowledge/{category}` | 某类知识详情 | 无 |
| `netpulse://interfaces` | 网卡列表 | D-Bus |
| `netpulse://health` | 健康快照 JSON | D-Bus |
| `netpulse://logs` | 日志文件列表 | 日志目录 |
| `netpulse://logs/{name}` | 某个日志文件内容 | 日志目录 |

前两个**不依赖任何外部服务**，所以在没起 C++ 服务、没建索引的情况下也能读。

### Prompts（3 个）

| 名字 | 参数 | 用途 |
|---|---|---|
| `diagnose_network` | `symptom`, `interface` | 按标准流程诊断 |
| `explain_metric` | `metric`, `value` | 解释某个指标数值 |
| `summarize_incident` | `time_range` | 总结某段时间发生了什么 |

---

## 七、接到客户端

### Claude Code

```bash
# 项目根目录建 .mcp.json（内容照抄 mcp_server/mcp.json.example，改成你的绝对路径）
# 或者用 CLI 一行加上：
claude mcp add netpulse -- python3 "<绝对路径>/mcp_server/server.py"
```

加完在 Claude Code 里输入 `/mcp`，应该看到 `netpulse` 是 `connected`。

### Claude Desktop

配置文件位置：

- Windows：`%APPDATA%\Claude\claude_desktop_config.json`
- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`

格式和上面 `mcp.json.example` 里的 `mcpServers` 块完全一样。改完重启客户端。

### 排查顺序（连不上时按这个顺序查）

1. **路径对不对** —— 配置文件里的绝对路径，Windows 上建议用正斜杠 `/`
2. **手动跑一遍**：`python server.py --demo --list` 能出东西吗？
   出不来就是 Python 环境问题，和 MCP 无关
3. **先用 `--demo`** —— 排除掉"C++ 服务没起"这个变量
4. 看客户端日志。Claude Code 可以用 `/mcp` 看连接状态

---

## 八、⚠️ stdio 模式的两个坑

### 坑一：stdout 就是协议通道

stdio 模式下，Server 和 Client 靠 **stdout** 传 JSON-RPC 帧。
**任何一条无关的 `print` 都会插进帧中间，把协议撕坏**，
而客户端报的错会完全指不到这里（通常是看不懂的 JSON 解析错误）。

**好消息**：MCP Python SDK 已经处理了这件事 —— 它在服务期间把 fd 1
重定向到 stderr（`mcp/server/stdio.py` 的 `_claim_fd`），应用侧的野生 print
会自动落到 stderr，不会污染协议。

> 这里我一开始判断错了。我 grep 了 `sys.stdout =` 和 `redirect_stdout`，
> 什么都没找到，于是以为 SDK 没保护。实际上它用的是 **fd 级**的
> `os.dup2`，grep 那样是看不到的。**结论：不要靠 grep 判断一个库做了什么，
> 要看它的文档字符串和实现。**

### 坑二：编码没被保护

SDK 保护了**通道**，没保护**编码**。

`sys.stdout` 这个对象本身没变，在简体中文 Windows 上它的 `encoding`
仍然是 GBK —— 所以**库代码里一个 emoji 的 print 照样会抛 UnicodeEncodeError**，
把一次工具调用搞崩。

所以 `server.py` 开头调了 `rag/console.py` 的 `enable_utf8_output()`，
并且顺手把 `rag/knowledge.py` 和 `rag/embedder.py` 里库级别的 emoji print
改成了纯文本 —— **库代码不该往一个不知道编码的控制台里写 emoji**。

> 这两个坑都可以在面试里讲：它们都不是"逻辑错误"，而是**环境差异导致的行为不一致**。
> 能定位到这类问题，比会写算法更能说明工程能力。

---

## 九、怎么测的（三层）

```bash
python test_server.py        # 69 项：进程内，测注册结果和逻辑
python test_e2e_stdio.py     # 37 项：起真子进程 + 真客户端，测传输链路
```

### 为什么需要两层

`test_server.py` 直接调 `server.call_tool()`，**绕过了整条传输层**。
它验证不了：

- 服务器进程能不能正常启动、能不能握手
- JSON-RPC 帧的编解码对不对
- stdout 有没有被野生 print 污染
- 参数经过一次真正的序列化/反序列化之后还对不对

这些恰恰是"我配好了但就是连不上"的常见原因，而**进程内测试永远看不出来**。

### 端到端测试怎么做到不依赖环境

`test_e2e_stdio.py` 起的是 `python server.py --demo` ——
假数据，不连 D-Bus、不读 FAISS、不需要 API Key。

**所以它在任何机器上都能跑通。** 这也是本项目反复用的模式：
把外部依赖抽成接口，测试就能完全离线。

### 重点测了什么

不是"注册上了没有"，而是几类**容易静默出错**的地方：

- **description 逐字一致** —— 桥接走样了，两条链路给模型看的东西就不同了
- **路径穿越防护** —— `netpulse://logs/{name}` 的 name 来自客户端，
  不检查的话传 `../../../../etc/passwd` 就能读到任意文件
- **外部依赖缺席时的表现** —— 是给出可读说明，还是抛异常
- **出错之后会话还活着** —— 传错参数不能让整个 MCP 会话崩掉

---

## 十、核心概念速查（面试）

### MCP 的三层

```
传输层   stdio / Streamable HTTP     —— 帧怎么发
协议层   JSON-RPC 2.0                —— 消息长什么样
原语层   tools / resources / prompts —— 表达什么
```

### 常见追问

**Q：MCP 和直接写 function calling 有什么区别？**
A：Function calling 是模型的能力，MCP 是工具的接入协议。MCP 客户端拿到工具列表后，
仍然通过 function calling 让模型决定调哪个 —— 两者互补。
MCP 解决的是 N×M 问题。

**Q：什么该做成 resource，什么该做成 tool？**
A：看谁控制。模型该自己决定什么时候用的 → tool；人该决定给不给模型看的 → resource。
本项目的知识库是 resource（静态参考），实时指标是 tool（模型要自己判断查不查）。

**Q：MCP Server 怎么保证安全？**
A：几个层面：① 资源 URI 要做路径穿越检查（本项目做了）；
② 工具参数要校验（复用 `agent/agent.py` 的 `_validate_arguments` 思路）；
③ stdio 模式下 Server 是客户端起的子进程，权限边界就是那个进程的权限 ——
**所以别把 MCP Server 跑在 root 下**。本项目读 eBPF 需要 root，
但那是 C++ 服务的事，MCP Server 本身以普通用户跑就行。

**Q：stdio 和 HTTP 传输怎么选？**
A：本地工具用 stdio（客户端起子进程，简单、无端口、天然隔离）；
远程或需要多客户端共享时用 Streamable HTTP。

---

## 十一、已知缺口

### ① 没做鉴权

HTTP 传输模式下没有认证 —— 任何人都能连上并读日志。
本项目的使用场景是**本机**（`--host` 默认 `127.0.0.1`），可接受；
真要对外暴露必须先加认证。

### ② 大文件读进内存

`netpulse://logs/{name}` 会一次读完（上限 200KB）。
日志大了应该做流式或分页 —— 目前够用，写在这里免得被问到时答不上来。

### ③ `--demo` 的数据是编的

`agent/offline_demo.py` 里所有数值都是为了演示编的，
**只能验证链路，不能用来判断网络好坏，更不能写进简历。**

### ④ 还没在真实客户端里点过

我验证到了"真 MCP 客户端通过 stdio 能完整驱动这个 Server"（37 项全过），
但**没有在 Claude Desktop 的 GUI 里实际操作过**。
你接上之后如果哪里不对，大概率是配置路径问题（见第七节的排查顺序）。

---

## 十二、和另外两个目录的关系

```
agent/        LLM 的"手脚" —— ReAct 循环 + 6 个工具
rag/          LLM 的"记忆" —— 混合检索 + Rerank
mcp_server/   把 agent/ 的工具按标准协议暴露出去（Week 3 Day 5-6）
```

**工具定义只在 `agent/tools.py` 里有一份**，`mcp_server/` 是它的另一种渲染。

Week 4 要做的是把这个 Server 包成 FastAPI + SSE 的 Web 服务 ——
届时会复用 `agent.py` 的 `on_event` 事件流（见 `agent/README.md` 第十节）。
