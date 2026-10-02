"""
Agent 工具层 —— 把 NetPulse 的采集能力包装成 LLM 可调用的 tool。

===========================================================================
本文件的核心设计：工具只定义一次，协议在边界处才决定
===========================================================================

Anthropic 协议的工具长这样：

    {"name": ..., "description": ..., "input_schema": {...}}

OpenAI 协议的工具长这样：

    {"type": "function", "function": {"name": ..., "description": ...,
                                      "parameters": {...}}}

两者只差一层皮：字段名不同、少一层嵌套。但如果你按其中一种把工具写死，
将来换协议就得把 N 个工具全改一遍 —— **而 description 是这个文件里最贵的
东西**，重写一遍必然出错。

所以这里定义一个与协议无关的 Tool，到发请求的那一刻才渲染：

    Tool.to_anthropic()   ->  Anthropic 形状
    Tool.to_openai()      ->  OpenAI 形状

本项目的实际情况正好说明了这个设计的必要性：百炼这边**一个 API Key 要打
三个不同协议的端点**（生成走 Anthropic、向量化走 OpenAI、重排走 DashScope
原生）。协议变来变去是这个项目的常态，不是意外。

===========================================================================
工具设计原则（面试会被问，也是 Agent 好不好用的分水岭）
===========================================================================

1. 一个 tool 对应一个【动作】，不是一个【数据切片】。

   HealthCheck 一次返回 11 个字段。按"每个指标一个 tool"来拆 —— 查丢包率、
   查 RTT、查 RSSI、查活跃连接 —— 每次诊断要调 4 次 D-Bus、烧 4 倍 token，
   拿到的还是同一份 JSON。所以合并成 get_conn_stats。

2. description 里必须写【什么时候调】，不只是【能干什么】。

   只写"返回网卡指标"，模型不知道该不该调；写上"当用户说网络慢、卡顿、
   延迟高但还没指明具体目标时先调这个"，它才会在正确的时候调。

3. description 里必须写【和相似工具怎么区分】。

   get_conn_stats 和 get_network_health 数据同源、语义完全不同：前者给原始
   指标，后者给结论。不写清楚，模型会随便挑一个。这类"看起来重复的工具"
   是工具设计里最容易翻车的地方，也是面试官最爱追问的点。

4. 错误也要返回 dict，绝不抛异常。

   见 ToolRunner.run() 的说明。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from netpulse_client import (
    LOSS_UNKNOWN,
    RTT_UNKNOWN,
    RSSI_NOT_WIFI,
    NetPulseClient,
    NetPulseError,
)

# ---------------------------------------------------------------------------
# 让本目录能 import 隔壁 rag/ 的东西
# ---------------------------------------------------------------------------
# rag/ 不是包（没有 __init__.py），没法用常规导入，所以显式挂到 sys.path 上。
#
# 这里是**模块级**插入而不是用的时候再插，有一个连带好处：agent/ 下的
# 入口脚本只要 import 了本模块，rag/ 就在路径里了，于是
# `from console import enable_utf8_output`（console.py 在 rag/ 下）
# 也能直接用 —— 不用为了一行编码修正把 console.py 复制第二份。
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_AGENT_DIR)
_RAG_DIR = os.path.join(_PROJECT_ROOT, "rag")
if _RAG_DIR not in sys.path:
    sys.path.insert(0, _RAG_DIR)


# ---------------------------------------------------------------------------
# 协议无关的工具描述
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """一个可被模型调用的工具。

    parameters 用 JSON Schema —— 这是两个协议的**公共子集**，不需要转换，
    只有外层包装不同。

    `method` 而不是 `handler`：具体的实现是 ToolRunner 上的方法，
    由 run() 通过 getattr 取出来调用。这样工具定义可以放在模块顶层当常量，
    不需要先有 runner 实例 —— 否则"定义工具要 runner，建 runner 要工具定义"
    就成了循环依赖。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    method: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_anthropic(self) -> dict[str, Any]:
        """Anthropic Messages 协议形状：扁平，schema 叫 input_schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
            **self.extra,
        }

    def to_openai(self) -> dict[str, Any]:
        """OpenAI / 百炼兼容模式形状：多一层 function，schema 叫 parameters。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# 工具定义表
# ---------------------------------------------------------------------------
#
# description 是**给模型读的**，所以用中文、用祈使句、写清触发条件和区别。
# 它同时是维护者理解这个工具定位的第一手材料 —— 两边合一，避免
# "注释里说的"和"模型看到的"不一致（那种不一致会让工具表现得很奇怪，
# 而你在代码里怎么看都正常）。
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="get_conn_stats",
        method="_get_conn_stats",
        description=(
            "获取本机网卡的实时连接与流量统计：当前活跃连接数、往返延迟(RTT)、"
            "TCP 丢包率、WiFi 信号强度、实时带宽与包速率、以及 0-100 的网络健康分。\n"
            "\n"
            "【什么时候调】用户说『网络慢』『卡顿』『延迟高』『断断续续』"
            "但还没指明具体目标时，先调这个拿到全局现状。\n"
            "\n"
            "【和别的工具怎么区分】要原始指标用这个；只要一句『好不好』的结论"
            "和问题清单，用 get_network_health；要定位到具体哪条连接在异常，"
            "用 get_flows。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "interface": {
                    "type": "string",
                    "description": (
                        "可选。只看指定网卡，例如 wlan0 或 eth0。"
                        "不填则返回当前上网网卡的指标。"
                    ),
                }
            },
            "required": [],
        },
    ),
    Tool(
        name="list_interfaces",
        method="_list_interfaces",
        description=(
            "列出本机所有网络接口名称（如 eth0、wlan0）。\n"
            "\n"
            "【什么时候调】不确定机器上有哪些网卡，或拿到指标后需要确认某块"
            "网卡是否真实存在、名字有没有拼错时调用。\n"
            "\n"
            "注意：服务端只上报【当前上网的那一块】网卡的详细指标，所以 "
            "get_conn_stats 传了 interface 却被忽略时，先用这个确认真实网卡名。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="ping_host",
        method="_ping_host",
        description=(
            "从本机主动 Ping 一个主机名或 IP，返回往返延迟。走服务端当前选定的"
            "上网网卡，3 秒超时。\n"
            "\n"
            "【什么时候调】用户提到了**具体的目标** —— 某个网站、IP、DNS 服务器、"
            "网关 —— 需要验证『到那儿通不通、延迟多少』时调用。\n"
            "\n"
            "【关键用法】这个工具能用来**验证推断**：怀疑是本地链路问题时，"
            "Ping 一个已知稳定的公网地址（如 223.5.5.5）—— 通且延迟正常，"
            "说明问题在目标站点；不通或延迟同样高，说明问题在本地。\n"
            "\n"
            "【和别的工具怎么区分】它测的是到某个远端目标的即时延迟，而 "
            "get_conn_stats 里的 rtt_ms 是系统周期性探测的结果。两个数值不一致"
            "本身就是有用的线索。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "hostname": {
                    "type": "string",
                    "description": (
                        "目标主机名或 IP 地址，例如 8.8.8.8、223.5.5.5 或 "
                        "www.baidu.com。不要带协议前缀，不要带端口。"
                    ),
                }
            },
            "required": ["hostname"],
        },
    ),
    Tool(
        name="get_network_health",
        method="_get_network_health",
        description=(
            "获取网络健康评估结论：0-100 的健康分、质量等级"
            "（优秀/良好/一般/差/未知）、以及系统自动检出的问题描述列表。\n"
            "\n"
            "【什么时候调】需要给出『网络到底好不好』的总结性判断，或需要一份"
            "现成的问题清单作为诊断起点时调用。\n"
            "\n"
            "【和别的工具怎么区分】这个只给结论和问题列表，不给原始指标。"
            "看到问题想知道具体数值是多少，再用 get_conn_stats。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_flows",
        method="_get_flows",
        description=(
            "列出当前最活跃的网络连接明细（五元组：源 IP、源端口、目的 IP、"
            "目的端口、协议），附带每条连接的累计字节数、包数和发起进程 PID。\n"
            "\n"
            "【什么时候调】已经知道『网络有问题』，现在要定位**是哪条连接造成的**\n"
            "时调用。这是从『现象』走到『根因』的关键一步：\n"
            "  · 带宽异常高 → 看是哪条连接在占带宽\n"
            "  · 怀疑异常外联或可疑进程 → 看有没有陌生的目的地址和 PID\n"
            "  · 丢包严重 → 看是不是某一条连接在刷流量\n"
            "\n"
            "【和别的工具怎么区分】get_conn_stats 给的是**每块网卡的聚合值**"
            "（比如活跃连接数 23 个），这个给的是**每条连接分别是多少**。"
            "前者回答『有没有问题』，后者回答『问题出在哪条连接上』。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "top_n": {
                    "type": "integer",
                    "description": "返回最活跃的前 N 条连接，默认 10，最大 50。",
                },
                "protocol": {
                    "type": "string",
                    "enum": ["TCP", "UDP"],
                    "description": "可选。只看某种协议，不填则 TCP 和 UDP 都返回。",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="recall_past_diagnoses",
        method="_recall_past_diagnoses",
        description=(
            "检索**你自己过去做过的诊断**，找出和当前问题相似的历史案例，"
            "返回当时的问题、结论和依据。\n"
            "\n"
            "【什么时候调】\n"
            "  · 用户用了『又』『还是』『上次那个问题』这类词，明显是复发\n"
            "  · 排查方向变了 —— 一开始怀疑延迟，后来怀疑信号 —— "
            "想用新的关键词重新检索一遍经验\n"
            "\n"
            "【和 search_network_history 怎么区分】这两个查的东西完全不同：\n"
            "  · search_network_history 查**日志原文和知识库** —— 客观事实\n"
            "  · 这个查**你以前得出的结论** —— 你当时怎么判断的\n"
            "  前者是资料，后者是经验。**经验可能是错的，参考时要保留怀疑。**\n"
            "\n"
            "注意：每次诊断开始时，系统已经自动注入了最相似的几条记忆，"
            "所以开场通常不用调。只有需要换个角度重新检索时才用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "检索用的关键词或现象描述。用你**现在的判断**去描述，"
                        "比如已经怀疑是信号问题就写『WiFi 信号弱 RSSI 下降』，"
                        "而不是照抄用户的原话。"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数，默认 5。",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="search_network_history",
        method="_search_network_history",
        description=(
            "在【历史监控日志】和【网络诊断知识库】里做混合检索"
            "（向量 + BM25 关键词 + Rerank 重排），返回最相关的资料片段。\n"
            "\n"
            "【什么时候调】两类场景：\n"
            "  1. 时间相关 —— 『刚才/10:02 的时候网络怎么样』、"
            "『历史上有没有类似的故障』、『信号是一直这么差还是刚变差的』\n"
            "  2. 知识相关 —— 『RTT 多少算严重』、『丢包率高一般什么原因』、"
            "『WiFi 信号弱该怎么处理』\n"
            "\n"
            "【怎么把 query 写好】带上关键限定词能显著提升准确率。"
            "『eth0 在 10:02 附近的 RTT 和丢包情况』远好于『网络怎么样』。\n"
            "\n"
            "【和别的工具怎么区分】这是唯一一个查**历史**和**通用知识**的工具。"
            "查此刻的实时指标请用 get_conn_stats —— 那个走 D-Bus 拿当前值，"
            "这个走 FAISS 检索已落盘的历史日志，数据来源完全不同。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "检索问题，用自然语言。带上网卡名、时间点、指标名"
                        "这些限定词效果最好。"
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回资料条数，默认 5，一般不用改。",
                },
            },
            "required": ["query"],
        },
    ),
]

TOOL_NAMES: list[str] = [t.name for t in TOOLS]

_LEVEL_NAMES = {0: "未知", 1: "差", 2: "一般", 3: "良好", 4: "优秀"}


# ---------------------------------------------------------------------------
# 执行层
# ---------------------------------------------------------------------------


class ToolRunner:
    """持有外部连接，把模型给的 (name, arguments) 分发到具体实现。

    两条铁律：

    1. **run() 永远返回 dict，永远不抛异常。**
       工具失败不是程序的 bug，是 Agent 必须处理的一种**正常输入**。
       模型看到 {"error": ...} 会自己修正参数重试；而异常穿透到循环外面，
       整个诊断就崩了 —— 模型连"我调错了"都不知道。

    2. **所有外部连接都是懒加载。**
       只跑测试、或只想用检索工具时，不该因为连不上 D-Bus 服务就起不来。
    """

    def __init__(self, client: NetPulseClient | None = None, memory: Any | None = None) -> None:
        self._client = client
        self._rag: Any | None = None
        self._memory = memory
        self._by_name = {t.name: t for t in TOOLS}

    # ---------------- 注册表访问 ----------------

    @property
    def tools(self) -> list[Tool]:
        return TOOLS

    @property
    def names(self) -> list[str]:
        return TOOL_NAMES

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [t.to_anthropic() for t in TOOLS]

    def openai_tools(self) -> list[dict[str, Any]]:
        return [t.to_openai() for t in TOOLS]

    # ---------------- 懒加载的外部依赖 ----------------

    @property
    def client(self) -> NetPulseClient:
        if self._client is None:
            self._client = NetPulseClient()
        return self._client

    @property
    def rag(self) -> Any:
        """检索层。第一次用到才建 FAISS 连接 / 加载索引。"""
        if self._rag is None:
            if _RAG_DIR not in sys.path:
                sys.path.insert(0, _RAG_DIR)
            from pipeline import NetworkRAG  # type: ignore

            self._rag = NetworkRAG.load(
                offline=os.environ.get("RAG_OFFLINE", "").lower() in ("1", "true", "yes"),
                use_rerank=True,
            )
        return self._rag

    @property
    def memory(self) -> Any:
        """长期记忆。同样懒加载 —— 不用记忆功能就完全不碰磁盘。"""
        if self._memory is None:
            from memory import LongTermMemory

            self._memory = LongTermMemory()
        return self._memory

    def bind_memory(self, memory: Any) -> None:
        """复用外部传入的记忆实例。

        为什么要显式绑：Agent 和工具层都要用记忆（Agent 在诊断前后注入/写入，
        工具层在模型主动检索时读）。如果两边各自 new 一个 LongTermMemory，
        就会有**两个对象各持一份记录**：Agent 写进去的，工具检索不到，
        因为那个实例的 `_records` 还是加载时的旧快照。

        现象是"明明刚诊断过，模型说没有历史记录" —— 很难查。
        """
        self._memory = memory

    # ---------------- 分发 ----------------

    def run(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行一个工具。任何情况下都返回 dict。"""
        arguments = arguments or {}
        tool = self._by_name.get(name)
        if tool is None:
            return {
                "error": f"没有名为 {name!r} 的工具。",
                "available_tools": self.names,
            }

        try:
            result = getattr(self, tool.method)(**arguments)
        except NetPulseError as exc:
            return {"error": str(exc)}
        except TypeError as exc:
            # 模型给了不存在的参数名，或必填项没给。
            # 这条消息会原样回灌给模型，所以把正确签名一起告诉它。
            return {
                "error": f"参数不匹配：{exc}",
                "correct_parameters": sorted(tool.parameters.get("properties", {})),
                "required": tool.parameters.get("required", []),
            }
        except Exception as exc:  # noqa: BLE001 —— 兜底，错误必须能回灌
            return {"error": f"{type(exc).__name__}: {exc}"}

        # 万一实现不小心返回了非 dict，包一层比让协议层炸掉好。
        if not isinstance(result, dict):
            return {"result": result}
        return result

    # ---------------- 各工具实现 ----------------
    #
    # 下面这些方法的注释是写给**维护者**的（实现细节、坑）；
    # 给**模型**看的 description 在模块顶部的 TOOLS 表里。两边分开是有意的 ——
    # 混在一起会让实现说明漏进 prompt，白烧 token 还干扰模型判断。

    def _get_conn_stats(self, interface: str | None = None) -> dict[str, Any]:
        """服务端 HealthCheck 只返回【当前上网的那一块】网卡的指标，不是所有网卡。
        这里按接口名过滤，并显式说明这一点 —— 否则模型会以为
        "所有网卡都正常"，得出错误结论。"""
        stats = _normalize(self.client.health_check())
        if interface and stats["interface"] != interface:
            return {
                "requested_interface": interface,
                "current_interface": stats["interface"],
                "note": (
                    f"服务端只上报当前上网网卡（{stats['interface']}），"
                    f"不是 {interface}。这不代表 {interface} 有问题，"
                    f"只是它没有实时数据。可先用 list_interfaces 确认网卡名。"
                ),
                "stats": stats,
            }
        return {"stats": stats}

    def _list_interfaces(self) -> dict[str, Any]:
        return {"interfaces": self.client.list_interfaces()}

    def _ping_host(self, hostname: str) -> dict[str, Any]:
        return self.client.ping(hostname)

    def _get_network_health(self) -> dict[str, Any]:
        stats = _normalize(self.client.health_check())
        return {
            "interface": stats["interface"],
            "quality_score": stats["quality_score"],
            "quality_level": stats["quality_level"],
            "issues": stats["issues"],
        }

    def _get_flows(self, top_n: int = 10, protocol: str | None = None) -> dict[str, Any]:
        """连接明细。

        同时给两种排序（按速率、按包速率），因为只给一种会让模型看不到
        "包很小但很频繁"这类连接 —— 那种连接往往是扫描或心跳异常的信号。
        """
        top_n = max(1, min(int(top_n or 10), 50))
        data = self.client.get_flows()

        flows = data.get("flows") or []
        if protocol:
            want = protocol.strip().upper()
            flows = [f for f in flows if str(f.get("protocol", "")).upper() == want]

        if not flows:
            return {
                "flows": [],
                "note": (
                    "采样窗口内没有采集到流量。可能原因：eBPF 未附加成功"
                    "（需要 root，且内核支持 BPF）、这段时间确实没有数据传输、"
                    "或流量全被接口过滤挡掉了。"
                    "**这不等于网络正常**，请结合其它工具判断。"
                ),
                "interface_filter": data.get("interface"),
            }

        return {
            "flow_count": len(flows),
            "returned": min(top_n, len(flows)),
            "note": (
                "bps/pps 是**采样窗口内的速率**（bytes/s、packets/s），不是累计量。"
                "只包含窗口内有过数据传输的连接，所以条数通常少于 "
                "get_conn_stats 里的 active_flows（那个是已建立的连接数）。"
            ),
            "interval_seconds": data.get("interval_seconds"),
            "top_by_bps": sorted(flows, key=lambda f: f.get("bps", 0), reverse=True)[:top_n],
            "top_by_pps": sorted(flows, key=lambda f: f.get("pps", 0), reverse=True)[:top_n],
            "interface_filter": data.get("interface"),
        }

    def _recall_past_diagnoses(self, query: str, top_k: int = 5) -> dict[str, Any]:
        """检索过往诊断结论。

        返回里**明确标注了这是过去的判断**，并且带上距今多久 ——
        因为一次两周前的结论和昨天的结论，可信度是不一样的。
        模型看到时间才能判断"这条经验还作数吗"。
        """
        top_k = max(1, min(int(top_k or 5), 20))
        try:
            hits = self.memory.recall(query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"长期记忆不可用：{type(exc).__name__}: {exc}"}

        if not hits:
            return {
                "memories": [],
                "note": (
                    "没有找到相似的历史诊断。可能是：还没有积累过类似案例、"
                    "或者记忆库是空的。这不代表这类问题没发生过。"
                ),
            }

        try:
            self.memory.mark_recalled([r for r, _ in hits])
        except Exception:  # noqa: BLE001
            pass

        return {
            "count": len(hits),
            "disclaimer": (
                "以下是你**过去做出的判断**，不是客观事实，也不是知识库内容。"
                "可以直接参考，但如果和当前实时数据冲突，以实时数据为准。"
            ),
            "memories": [
                {
                    "date": r.created_at[:10],
                    "question": r.question,
                    "conclusion": r.conclusion,
                    "interfaces": r.interfaces,
                    "occurrences": r.occurrences,
                    "similarity": round(score, 3),
                }
                for r, score in hits
            ],
        }

    def _search_network_history(self, query: str, top_k: int = 5) -> dict[str, Any]:
        try:
            return self.rag.as_tool(query, top_k=int(top_k or 5))
        except RuntimeError as exc:
            # 索引没建。pipeline.load() 的报错里已经带了可执行的下一步。
            return {"error": str(exc)}
        except ImportError as exc:
            return {
                "error": (
                    f"检索层不可用：{exc}\n"
                    f"请确认 {_RAG_DIR} 存在，并已执行 "
                    f"`python build_index.py --sample` 建好索引。"
                )
            }


# ---------------------------------------------------------------------------
# 哨兵值翻译
# ---------------------------------------------------------------------------


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """把服务端的哨兵值翻译成人类/模型能理解的东西。

    服务端用 -1000 表示"非 WiFi 网卡"、-1 表示"还没测出来"。
    直接把这些数字丢给模型，它会当成真的读数值 ——
    **"RSSI 是 -1000 dBm"会被理解成信号极差**，而实际上那是一块有线网卡。

    这是最典型的"数据没错、语义错了"的坑：JSON 里每个字段都是合法数字，
    没有任何一层会报错，只有模型的结论会莫名其妙地离谱。
    """
    rssi = raw.get("rssi_dbm", RSSI_NOT_WIFI)
    loss = raw.get("tcp_loss_rate", LOSS_UNKNOWN)
    rtt = raw.get("rtt_ms", RTT_UNKNOWN)
    level = raw.get("quality_level", 0)

    return {
        "interface": raw.get("interface"),
        "using_now": bool(raw.get("using_now", False)),
        "quality_score": raw.get("quality_score"),
        "quality_level": _LEVEL_NAMES.get(level, "未知"),
        "rtt_ms": None if rtt == RTT_UNKNOWN else rtt,
        "tcp_loss_rate_pct": None if loss == LOSS_UNKNOWN else loss,
        "rssi_dbm": None if rssi == RSSI_NOT_WIFI else rssi,
        "traffic_bps": raw.get("traffic_bps"),
        "traffic_pps": raw.get("traffic_pps"),
        "active_flows": raw.get("active_flows"),
        "issues": raw.get("issues", []),
    }


if __name__ == "__main__":
    import json

    from console import enable_utf8_output

    enable_utf8_output()

    runner = ToolRunner()
    print(f"已注册 {len(runner.tools)} 个工具：\n")
    for t in runner.tools:
        required = t.parameters.get("required", [])
        optional = [k for k in t.parameters.get("properties", {}) if k not in required]
        sig = ", ".join(list(required) + [f"{o}=?" for o in optional])
        print(f"  {t.name}({sig})")
        print(f"      description {len(t.description)} 字")
    print()
    print("同一个工具，两种协议的渲染：")
    print("  Anthropic:", json.dumps(runner.anthropic_tools()[2], ensure_ascii=False)[:150], "...")
    print("  OpenAI   :", json.dumps(runner.openai_tools()[2], ensure_ascii=False)[:150], "...")
    print()
    print("错误路径检查（必须返回 dict，不能抛异常）：")
    print("  未知工具 ->", runner.run("no_such_tool", {}))
    print("  错误参数 ->", runner.run("ping_host", {"wrong_arg": 1}))
    print("  缺必填项 ->", runner.run("ping_host", {}))
