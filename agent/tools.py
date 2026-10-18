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
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from netpulse_client import (
    LOSS_UNKNOWN,
    RTT_UNKNOWN,
    RSSI_NOT_WIFI,
    NetPulseClient,
    NetPulseError,
)

# host_bridge（WSL 宿主机 WiFi 桥接）是纯标准库、跨平台的，但它只在对端
# （powershell.exe）存在时才有意义。同样惰性导入：本文件被单独拷走、或跑在
# 原生 Linux 上时，缺它不该让整个工具层起不来。
#
# 注意：netpulse_client 里也 import 了它，两边是同一个模块对象 ——
# 测试里 patch tools.host_bridge 即可替换（见 test_agent.py 的 [N] 节）。
try:
    import host_bridge
except ImportError:  # pragma: no cover
    host_bridge = None  # type: ignore[assignment]

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
            "【WSL 环境必读】WSL 虚拟机只能看到虚拟网卡 eth0，真实上网出口是 "
            "Windows 宿主 WiFi。此时返回的 wifi 字段是宿主 WiFi 的完整指标："
            "SSID、信号百分比与估算 RSSI(dBm)、信道/频段、认证方式、链路速率、"
            "RTT、抖动、丢包率、下载/上传速率、IPv4 连通性。"
            "判断用户真实上网质量要以 wifi 字段为准。\n"
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
            "【WSL 环境】返回里带 real_uplink 字段时，那是真实上网出口"
            "（Windows 宿主 WiFi）的实测 vital：RTT、抖动、丢包率、"
            "下载/上传速率。判断用户真实上网质量以它为准。\n"
            "\n"
            "【和别的工具怎么区分】这个主要给结论和问题列表。要**当场新鲜的"
            "四项实测值**（延迟/抖动/下载/上传），用 measure_network_performance；"
            "要每块网卡的原始聚合指标，用 get_conn_stats。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="measure_network_performance",
        method="_measure_network_performance",
        description=(
            "**主动实测**当前上网出口的指标：网络延迟(RTT)、延迟抖动、丢包率、"
            "当前下载/上传速率；mode='speed' 时额外测得**链路能力**"
            "（对标测速网站的 Mbps 数）。quick 约 5-8 秒，speed 约 10-16 秒。\n"
            "\n"
            "【什么时候调】用户明确要求『测一下』『现在多少』『帮我测网速』"
            "『延迟/抖动/下载/上传速度是多少』——要**当场的新鲜数值**时用这个。\n"
            "\n"
            "【mode 怎么选】用户问『网速多少』『带宽多少 Mbps』『下载/上传"
            "能到多少』（对标测速网站的那种数）→ **必须 mode='speed'**："
            "多线程主动打满链路实测能力值。用户问『现在卡不卡』『延迟多少』"
            "『网络正常吗』→ mode='quick'（默认）：一次全新采样，不打流。\n"
            "\n"
            "【两个速率不是一回事，汇报时必须区分】download_bps/upload_bps "
            "是网卡字节计数器差值 = **此刻的实时流量**（闲着时就几十 KB/s），"
            "不是宽带能力；download_capacity_bps/upload_capacity_bps 才是"
            "mode='speed' 主动测得的链路能力。把实时流量当成用户宽带速度"
            "汇报是错的（实测踩过：33 KB/s vs 测速网站 58.6 Mbps）。\n"
            "\n"
            "【和别的工具怎么区分】get_conn_stats / get_network_health 读的是"
            "服务端周期性监控（约 10s 前）的缓存值，回答『有没有问题』；"
            "这个工具发起一次全新测量，回答『此刻到底是多少』。\n"
            "\n"
            "【WSL 环境】测的是 Windows 宿主 WiFi 这条真实上网出口，"
            "不是 WSL 虚拟网卡。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "可选。ping 的目标，默认 223.5.5.5。"
                        "用户指定了目标（某个网站/网关）时才填。"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["quick", "speed"],
                    "description": (
                        "quick（默认）：一次全新采样拿四项实时值，约 5-8s，不打流。"
                        "speed：额外多线程打满链路 4s×2（下载/上传）测能力值，"
                        "对标测速网站，约 10-16s，会真实消耗几十到几百 MB 流量。"
                        "用户问网速/带宽 Mbps 时必须选 speed。"
                    ),
                },
            },
            "required": [],
        },
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
    def client(self) -> Any:
        """指标数据源。按环境选一条通路：

        1. ``NETPULSE_LITE=1`` → 纯 Python 采集（psutil + ping），
           跨平台、无需编译/eBPF/D-Bus（见 lite_collector.py）
        2. 平台上根本没有 dbus-python（Windows/macOS）且有 psutil →
           自动走轻量模式。否则那台上每个工具都只会报"D-Bus 是 Linux
           专有的"，对一台没装 WSL 的电脑毫无意义
        3. 其它 → NetPulseClient（完整模式：C++ 服务端 + eBPF + D-Bus）
        """
        if self._client is None:
            if os.environ.get("NETPULSE_LITE", "").lower() in ("1", "true", "yes"):
                from lite_collector import LiteCollector

                self._client = LiteCollector()
            else:
                import netpulse_client as _npc

                if _npc.dbus is None:
                    # dbus 在这台机器上永远装不上 → 有 psutil 就自动降级。
                    # lite_collector 本身缺文件时让 NetPulseClient 抛出
                    # 那条带完整指引的错（装 dbus / 用 WSL），别在这里另写一套。
                    try:
                        from lite_collector import LiteCollector

                        self._client = LiteCollector()
                    except ImportError:
                        self._client = NetPulseClient()
                else:
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
        "所有网卡都正常"，得出错误结论。

        WSL 下返回里可能带 uplink_note / wifi_bridge_error（见
        netpulse_client._merge_host_wifi）：前者说明真实上网出口是宿主
        WiFi、指标在 stats.wifi 里；后者说明桥接失败的原因。两者都必须
        原样透传给模型——藏掉任何一个都会让结论失去依据。"""
        raw = self.client.health_check()
        stats = _normalize(raw)
        result: dict[str, Any] = {"stats": stats}
        if raw.get("uplink_note"):
            result["uplink_note"] = raw["uplink_note"]
        if raw.get("wifi_bridge_error"):
            result["wifi_bridge_error"] = raw["wifi_bridge_error"]
        if interface and stats["interface"] != interface:
            result.update(
                {
                    "requested_interface": interface,
                    "current_interface": stats["interface"],
                    "note": (
                        f"服务端只上报当前上网网卡（{stats['interface']}），"
                        f"不是 {interface}。这不代表 {interface} 有问题，"
                        f"只是它没有实时数据。可先用 list_interfaces 确认网卡名。"
                    ),
                }
            )
        return result

    def _list_interfaces(self) -> dict[str, Any]:
        return {"interfaces": self.client.list_interfaces()}

    def _ping_host(self, hostname: str) -> dict[str, Any]:
        return self.client.ping(hostname)

    def _get_network_health(self) -> dict[str, Any]:
        raw = self.client.health_check()
        stats = _normalize(raw)
        result: dict[str, Any] = {
            "interface": stats["interface"],
            "quality_score": stats["quality_score"],
            "quality_level": stats["quality_level"],
            "issues": stats["issues"],
        }
        # 宿主 WiFi（WSL 桥接）的实测 vital：延迟/抖动/丢包/上下行速率。
        #
        # ⚠️ 这里曾经只在 uplink_note 存在时才带出来，而 uplink_note 的触发
        # 条件是"服务端上报主体还是 eth0"。C++ 服务端重编、wlan0 成为上报
        # 主体之后，uplink_note 再也不出现——于是这些指标**凭空消失**，
        # 模型回答"无法获取延迟/下载速度"，而数据其实一直都在 raw["wifi"] 里。
        # 判据必须是 wifi 块在不在，与 uplink_note 无关。
        wifi = raw.get("wifi") or {}
        if wifi:
            result["real_uplink"] = {
                "interface": wifi.get("interface", "wlan0"),
                "ssid": wifi.get("ssid"),
                "signal_pct": wifi.get("signal_pct"),
                "rssi_dbm": wifi.get("rssi_dbm"),
                "rtt_ms": wifi.get("rtt_ms"),
                "jitter_ms": wifi.get("jitter_ms"),
                "tcp_loss_pct": wifi.get("tcp_loss_pct"),
                "download_bps": wifi.get("download_bps"),
                "upload_bps": wifi.get("upload_bps"),
                "note": "以上是真实上网出口（Windows 宿主 WiFi）的实测值",
            }
        # WSL 且服务端未重编时，上面的分数是 WSL 虚拟网卡（eth0）的，
        # 不是用户真实上网出口（宿主 WiFi）的。把 WiFi 的关键 vitals 附上，
        # 否则模型会把"eth0 92 分"当成"用户 WiFi 很好"——那是错的结论。
        if raw.get("uplink_note"):
            result["uplink_note"] = raw["uplink_note"]
        if raw.get("wifi_bridge_error"):
            result["wifi_bridge_error"] = raw["wifi_bridge_error"]
        return result

    def _measure_network_performance(
        self, target: str | None = None, mode: str = "quick"
    ) -> dict[str, Any]:
        """主动实测：延迟(RTT)、抖动、丢包率、当前上下行速率；可选链路能力。

        mode="quick"（默认）：一次**全新的**宿主桥接采样（WSL）或服务端
        周期采样（原生 Linux），约 5~8 秒。速率来自网卡字节计数器差值——
        **是此刻的实时流量，不是宽带能力**。

        mode="speed"：在 quick 之上，再主动把链路打满（下载/上传各约 4s、
        4 线程），测得的能力值对标测速网站。约 10-16s，会真实消耗几十到
        几百 MB 流量。用户问"网速多少 Mbps / 下载速度能到多少"（对标测速
        网站）时必须用这个 mode——quick 的实时流量在这种问题下会误导。
        """
        tgt = (target or "").strip() or None
        mode = (mode or "quick").strip().lower()
        if mode not in ("quick", "speed"):
            mode = "quick"

        # 服务端侧数据：HealthCheck 的 wifi 块（C++ 桥接每 10s 刷新）。
        # 它有两个作用：速率基准的兜底，以及原生 Linux 的唯一来源。
        server_wifi: dict[str, Any] = {}
        try:
            server_wifi = self.client.health_check().get("wifi") or {}
        except NetPulseError:
            server_wifi = {}

        live: dict[str, Any] | None = None
        bridge_error = ""
        if host_bridge is not None and host_bridge.is_wsl():
            live = host_bridge.query_host_wifi(ping_target=tgt, use_cache=False)
            if not live.get("ok"):
                bridge_error = str(live.get("error", "宿主机桥接查询失败"))
                live = None

        if live is None and not server_wifi:
            # speed 模式的能力值实测不依赖桥接/服务端（就是本地 HTTP 打流），
            # 所以哪怕拿不到实时指标，也该把能力值测出来再失败——
            # "服务端没起"和"网络不通"是两回事，别把前者报成后者。
            if mode == "speed":
                speed = _active_bandwidth_test()
                if speed.get("ok"):
                    result: dict[str, Any] = {
                        "ok": True,
                        "measured_via": "active_bandwidth_test_only",
                        "note": (
                            "拿不到上网出口的实时指标（宿主桥接与服务端都无数据），"
                            "仅返回主动测得的链路能力；延迟/抖动/丢包不可用。"
                        ),
                        "speed_test": speed,
                        "download_capacity_bps": speed.get("download_capacity_bps"),
                        "upload_capacity_bps": speed.get("upload_capacity_bps"),
                    }
                    result["summary"] = _format_perf_summary(result)
                    return result
            return {
                "ok": False,
                "error": bridge_error or "拿不到上网出口的实测数据",
                "hint": (
                    "WSL 上检查 powershell.exe / netsh 是否可用；"
                    "原生 Linux 上确认有真实 wlan0 且服务端在跑。"
                ),
            }

        src = live if live is not None else server_wifi
        download = src.get("download_bps")
        upload = src.get("upload_bps")

        # 首次采样没有基准样本时 rate_basis=no_baseline、速率为 0 ——
        # 那不是"网络没流量"，是"算不出来"。服务端从启动起就在采样，
        # 它的值可以补这个缺口；两边都没有才老老实实报 0 并说明原因。
        rate_note = ""
        if live is not None and live.get("rate_basis") == "no_baseline":
            d2, u2 = server_wifi.get("download_bps"), server_wifi.get("upload_bps")
            if d2 or u2:
                download, upload = d2, u2
                rate_note = "本次采样之前无基准样本，速率取自服务端持续采样值"
            else:
                rate_note = "首次采样无基准样本，速率显示 0；稍后再次调用可得真实值"

        rtts = [int(v) for v in (src.get("rtt_samples") or []) if str(v).lstrip("-").isdigit()]
        result: dict[str, Any] = {
            "ok": True,
            "measured_via": (
                "fresh_host_bridge_sample" if live is not None else "server_periodic_sample"
            ),
            "interface": src.get("interface", "wlan0"),
            "ssid": src.get("ssid"),
            "target": src.get("ping_target") or tgt or "223.5.5.5",
            # ---- 用户要的四项 ----
            "rtt_ms": src.get("rtt_ms"),
            "jitter_ms": src.get("jitter_ms"),
            "download_bps": download,
            "upload_bps": upload,
            # ---- 佐证 ----
            "tcp_loss_pct": src.get("tcp_loss_pct"),
            "rtt_samples": rtts,
            "signal_pct": src.get("signal_pct"),
            "rssi_dbm": src.get("rssi_dbm"),
            "link_rx_mbps": src.get("link_rx_mbps") or src.get("receive_mbps"),
            "link_tx_mbps": src.get("link_tx_mbps") or src.get("transmit_mbps"),
            "ipv4_connectivity": src.get("ipv4_connectivity"),
        }
        if rate_note:
            result["rate_note"] = rate_note
        if bridge_error:
            result["bridge_error"] = bridge_error
        # 速率语义钉死在结果里：用户实测踩过的坑——33 KB/s 的实时流量被
        # 当成"宽带只有 33 KB/s"汇报，而测速网站显示 58.6 Mbps。两个数
        # 都是真的，只是量的不是同一个东西。模型转述时照抄这段就行。
        result["rate_semantics"] = (
            "download_bps/upload_bps = 网卡字节计数器差值 = 此刻正在过网卡的"
            "实时流量（利用率，闲着时就很低），不是宽带能力；"
            "要测链路能力（对标测速网站的 Mbps 数）需以 mode='speed' 重新调用。"
        )
        if mode == "speed":
            speed = _active_bandwidth_test()
            result["speed_test"] = speed
            if speed.get("ok"):
                result["download_capacity_bps"] = speed.get("download_capacity_bps")
                result["upload_capacity_bps"] = speed.get("upload_capacity_bps")
            else:
                # 测速失败不该让整次调用失败：四项实时值仍然有效。
                result["speed_test_error"] = (
                    speed.get("error") or "主动测速失败（端点不可达或不通）"
                )
        result["summary"] = _format_perf_summary(result)
        return result

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
# 主动带宽实测（对标测速网站）：多线程打流固定时长，数字节
# ---------------------------------------------------------------------------
#
# 为什么需要它：网卡字节计数器差值（download_bps/upload_bps）回答的是
# "此刻有多少数据正在过网卡"——利用率；而用户问"我网速多少"要的是
# "这条链路最多能跑多快"——能力。刷着网页时前者是 33 KB/s，
# 后者可能是 58 Mbps，差 200 倍，两者根本不是一回事。
# 测速网站的做法就是这里的做法：多线程 + 固定时长窗口 + 数字节。
#
# 代价：会真实打满链路几秒（默认 4 线程 × 4s，百兆约 50MB、千兆约 500MB），
# 所以是 opt-in（mode="speed"），不放进默认路径。

_DEFAULT_SPEED_BASE = "https://speed.cloudflare.com"


def _active_bandwidth_test(
    duration_s: float = 4.0,
    threads: int = 4,
    base_url: str | None = None,
) -> dict[str, Any]:
    """多线程下载/上传各跑一个固定时长窗口，用真实吞吐推链路能力。

    端点默认 Cloudflare 公开测速接口（__down / __up，全球 Anycast，
    教育网/运营商基本都可达）。用户网络到 Cloudflare 不通时，用环境变量
    NETPULSE_SPEED_BASE_URL 指到同类接口（比如校内测速服务器）即可，
    代码不用动。

    窗口里含建连与 TCP 慢启动时间，且默认只有 4s（测速网站通常 10-15s），
    所以读数可能比测速网站略低——量级对得上，不是同一秒数。
    """
    import threading

    base = (
        base_url or os.environ.get("NETPULSE_SPEED_BASE_URL") or _DEFAULT_SPEED_BASE
    ).rstrip("/")
    duration = max(2.0, min(float(duration_s or 4.0), 15.0))
    n = max(1, min(int(threads or 4), 8))

    try:
        import requests
    except ImportError:  # pragma: no cover —— requirements 里有；真缺了不该炸
        return {"ok": False, "error": "缺少 requests 库，无法主动测速"}

    dl_bytes = [0]
    ul_bytes = [0]
    dl_err: list[str] = []
    ul_err: list[str] = []
    ul_stop = [0.0]  # 上传"停止产出"的时刻，见下面 _phase 后的计时修正

    def _dl_worker() -> None:
        try:
            deadline = time.monotonic() + duration
            # Cloudflare 的 __down 单次请求上限 10MB（实测 >10MB 直接 403），
            # 所以用 Session 保持连接、循环要下一段——既绕过单次上限，
            # 又让 TCP/TLS 握手只在第一次付一次成本（保活复用）。
            url = f"{base}/__down?bytes=10000000"
            with requests.Session() as sess:
                while time.monotonic() < deadline:
                    with sess.get(url, stream=True, timeout=(5, 5)) as resp:
                        resp.raise_for_status()
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                dl_bytes[0] += len(chunk)
                            if time.monotonic() >= deadline:
                                break
                    if time.monotonic() >= deadline:
                        break
        except Exception as exc:  # noqa: BLE001 —— 单线程失败不该炸掉整个测速
            dl_err.append(f"{type(exc).__name__}: {exc}")

    def _ul_worker() -> None:
        try:
            deadline = time.monotonic() + duration

            def _body():
                block = b"\x00" * (262144)
                while time.monotonic() < deadline:
                    ul_bytes[0] += len(block)
                    yield block
                ul_stop[0] = time.monotonic()

            # 生成器做 body → requests 自动用 chunked 编码，服务端边收边丢，
            # 和测速网站的上传测试同一个原理。字节数按"交给传输层"计，
            # 与下载侧按"读到的字节"计存在毫秒级错位，可忽略。
            resp = requests.post(f"{base}/__up", data=_body(), timeout=(5, 10))
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            ul_err.append(f"{type(exc).__name__}: {exc}")

    def _phase(workers: list) -> tuple[float, float]:
        started = time.monotonic()
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        return started, time.monotonic() - started

    _dl_started, dl_elapsed = _phase(
        [threading.Thread(target=_dl_worker, daemon=True) for _ in range(n)]
    )
    ul_started, ul_elapsed = _phase(
        [threading.Thread(target=_ul_worker, daemon=True) for _ in range(n)]
    )
    # 上传计时要截至"生成器停止产出"（=deadline），不含之后等服务端
    # 响应的 drain 时间——那段时间一个字节都没在传，计进去会低估吞吐
    # （实测 3s 窗口被计成 4.7s，109 Mbps 实际约 170 Mbps）。
    if ul_stop[0] > ul_started:
        ul_elapsed = max(ul_stop[0] - ul_started, 0.1)

    def _bps(total: int, elapsed: float) -> float | None:
        return round(total * 8 / elapsed, 0) if total > 0 and elapsed > 0.1 else None

    out: dict[str, Any] = {
        "ok": bool(dl_bytes[0] or ul_bytes[0]),
        "url": base,
        "duration_s": duration,
        "threads": n,
        "download_bytes": dl_bytes[0],
        "upload_bytes": ul_bytes[0],
        "download_capacity_bps": _bps(dl_bytes[0], dl_elapsed),
        "upload_capacity_bps": _bps(ul_bytes[0], ul_elapsed),
    }
    if dl_err:
        out["download_error"] = dl_err[0]
    if ul_err:
        out["upload_error"] = ul_err[0]
    out["note"] = (
        "能力值 = 多线程主动打满链路测得的吞吐（对标测速网站），"
        "与 download_bps/upload_bps（网卡计数器的实时流量）不是一回事；"
        "窗口比测速网站短（默认 4s vs 10-15s）且含建连时间，读数可能略偏低。"
    )
    return out


# ---------------------------------------------------------------------------


def _format_perf_summary(m: dict[str, Any]) -> str:
    """把四项指标拼成一句人话，模型可以直接转述给用户。

    为什么要多此一举：模型拿到散字段后经常自己算错单位
    （把 B/s 说成 MB/s），或者漏报一两项。给一句拼好的 summary，
    它照抄就行，四项一个都不少。

    实时流量和能力值必须分开说——这是用户实测踩过的坑：
    33 KB/s 的实时流量被当成"宽带只有 33 KB/s"汇报，而测速网站
    显示 58.6 Mbps。两个数都是真的，只是量的不是同一个东西。
    """

    def rate(bps: Any) -> str:
        if bps is None:
            return "未知"
        v = float(bps)
        if v >= 1024 * 1024:
            return f"{v / 1024 / 1024:.2f} MB/s"
        if v >= 1024:
            return f"{v / 1024:.1f} KB/s"
        return f"{v:.0f} B/s"

    def mbps(bps: Any) -> str:
        if bps is None:
            return "未知"
        # 能力值用十进制 Mbps（1 Mbps = 1e6 bps）——运营商和测速网站
        # 都这么报；实时流量的 B/s 用 1024 进制。两个单位刻意不同，
        # 因为报的本来就是两种东西。
        return f"{float(bps) / 1_000_000:.1f} Mbps"

    rtt = m.get("rtt_ms")
    jitter = m.get("jitter_ms")
    loss = m.get("tcp_loss_pct")
    parts = [
        f"延迟 {rtt if rtt is not None else '未知'} ms",
        f"抖动 {jitter if jitter is not None else '未知'} ms",
        f"丢包 {loss if loss is not None else '未知'}%",
        f"当前实时流量：下载 {rate(m.get('download_bps'))}、"
        f"上传 {rate(m.get('upload_bps'))}"
        "（网卡计数器差值=此刻有多少数据在过网卡，不是宽带能力）",
    ]
    if m.get("download_capacity_bps") is not None or m.get("upload_capacity_bps") is not None:
        parts.append(
            "链路能力（多线程主动打满实测，对标测速网站）："
            f"下载 {mbps(m.get('download_capacity_bps'))}、"
            f"上传 {mbps(m.get('upload_capacity_bps'))}"
        )
    return "，".join(parts)


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
        "wifi": raw.get("wifi"),  # WSL 桥接：宿主 WiFi 全量指标（ssid/signal_pct/rssi_dbm/
        #   channel/band/authentication/receive_mbps/rtt_ms/jitter_ms/
        #   tcp_loss_pct/download_bps/upload_bps/ipv4_connectivity/...），
        #   字段定义见 agent/host_bridge.py 的 PowerShell 脚本
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
