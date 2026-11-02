"""
离线演示脚手架 —— 不用 API Key、不用 dbus、不用起 C++ 服务，
就能把整条 Agent 链路和推理轨迹跑出来。

    python main.py --dry-run

===========================================================================
这东西不是"玩具"，它解决的是一个很实际的问题
===========================================================================

学习 Agent 时最大的障碍是：**你没法一边调代码一边观察循环**。

真模型有不确定性（同一句话两次跑不一样）、要花钱、要配 Key、要等网络；
D-Bus 那边还要 root、要编译、要内核支持 eBPF。任何一环没通，
你就看不到"循环到底是怎么转的"。

把这两头都换成确定性的假实现之后，中间那段 —— 循环、容错、轨迹 ——
就完全裸露出来了。**先看清楚循环长什么样，再去接真实依赖。**
这个顺序比反过来快得多。

另外它也是面试材料：能说清"我把外部依赖抽成了接口，所以能在 CI 里
确定性地测一个依赖外部 API 的 Agent"，比说"我加了容错"具体得多。

===========================================================================
重要：假数据是编的
===========================================================================

下面 FAKE 里的数值是**为了演示编出来的**，不是真实测量结果。
它只用来验证"链路通不通"，不能用来判断网络好不好，更不能写进简历。
"""

from __future__ import annotations

from typing import Any

from llm_client import ToolCall, Turn
from tools import ToolRunner


def default_demo_script() -> list[Turn]:
    """剧本：演一遍典型的多步诊断。

    刻意照着简历上那条工具链走：

        健康分评估 → 定位异常连接 → 主动探测验证 → 历史日志关联

    这样你跑 --dry-run 时看到的轨迹，和简历里描述的工作方式是一致的。
    """
    return [
        # 第 1 轮：先看整体状况，不要一上来就查细节
        Turn(
            text=(
                "用户说网络卡。卡可能是延迟高、丢包，也可能是带宽被占满了。"
                "先调 get_network_health 拿结论和问题清单，再决定往哪个方向查。"
            ),
            tool_calls=[ToolCall(id="call_1", name="get_network_health", arguments={})],
            stop_reason="tool_use",
        ),
        # 第 2 轮：拿到问题清单后要具体数值
        Turn(
            text=(
                "健康分 64 分（一般），问题清单里有『RTT 偏高』和『TCP 丢包率 3.2%』两项。"
                "现在需要具体数值来判断严重程度，调 get_conn_stats。"
            ),
            tool_calls=[ToolCall(id="call_2", name="get_conn_stats", arguments={})],
            stop_reason="tool_use",
        ),
        # 第 3 轮：从"网卡有问题"走到"哪条连接有问题"
        Turn(
            text=(
                "RTT 210ms、丢包 3.2%、带宽 2.7MB/s、活跃连接 23 个。"
                "带宽不算饱和，所以不像单纯拥塞。接下来要定位是哪条连接在传数据，"
                "调 get_flows 看连接明细。"
            ),
            tool_calls=[ToolCall(id="call_3", name="get_flows", arguments={"top_n": 5})],
            stop_reason="tool_use",
        ),
        # 第 4 轮：主动探测 —— 区分"本地问题"和"对端问题"
        Turn(
            text=(
                "最活跃的连接是到 223.5.5.5 的。关键一步：为了区分是本地链路问题"
                "还是目标站点问题，主动 Ping 一个已知稳定的公网地址。"
                "如果 Ping 也慢，说明问题在本地。"
            ),
            tool_calls=[
                ToolCall(id="call_4", name="ping_host", arguments={"hostname": "223.5.5.5"})
            ],
            stop_reason="tool_use",
        ),
        # 第 5 轮：关联历史
        Turn(
            text=(
                "主动 Ping 205ms，和系统上报的 RTT 210ms 基本一致 —— "
                "两个独立来源都指向本地链路。现在查历史日志：这个现象是刚出现的"
                "还是一直如此？顺便查一下这类问题的一般排查方向。"
            ),
            tool_calls=[
                ToolCall(
                    id="call_5",
                    name="search_network_history",
                    arguments={"query": "eth0 RTT 升高 和 TCP 丢包 的历史记录与排查方式"},
                )
            ],
            stop_reason="tool_use",
        ),
        # 第 6 轮：收尾
        Turn(
            text=(
                "## 根因判断\n\n"
                "eth0 网卡的 RTT 已升到 210ms，同时 TCP 丢包率 3.2%，健康分 64 分。\n"
                "关键证据是：**主动 Ping 的延迟（205ms）与系统上报的 RTT（210ms）"
                "高度一致** —— 两个独立来源同时偏高，说明问题出在本地链路，"
                "而不是对端站点。\n\n"
                "带宽 2.7MB/s、活跃连接 23 个，都属于中等水平，不足以单独解释"
                "这么高的延迟，所以更像链路质量问题而非纯粹的流量拥塞。\n\n"
                "## 依据\n\n"
                "· 健康分 64 分 / 丢包率 3.2% / RTT 210ms —— 来自本机实时采集\n"
                "· 主动 Ping 205ms —— 与系统 RTT 交叉验证，排除对端问题\n"
                "· 历史日志中检出同类现象，说明不是一次性抖动\n\n"
                "## 建议\n\n"
                "1. 检查 eth0 的物理链路和交换机端口协商速率（是否有降速/半双工）\n"
                "2. 确认该时段是否有大流量传输与丢包同时发生\n"
                "3. 若持续存在，用抓包确认是否存在重传集中，进一步区分是链路丢包"
                "还是设备队列溢出"
            ),
            stop_reason="end_turn",
        ),
    ]


class DemoToolRunner(ToolRunner):
    """把所有工具短路成本地假数据的 runner。

    只覆盖 run() 一个方法，而不是去实现各个工具 —— 这样这条演示路径
    既不连 D-Bus、也不读 FAISS、更不花一分钱额度。
    """

    #: ⚠️ 演示数据，编的。见模块开头说明。
    FAKE: dict[str, dict[str, Any]] = {
        "get_network_health": {
            "interface": "eth0",
            "quality_score": 64.0,
            "quality_level": "一般",
            "issues": ["RTT 偏高（210ms）", "TCP 丢包率 3.2%"],
        },
        "get_conn_stats": {
            "stats": {
                "interface": "eth0",
                "using_now": True,
                "quality_score": 64.0,
                "quality_level": "一般",
                "rtt_ms": 210,
                "tcp_loss_rate_pct": 3.2,
                "rssi_dbm": None,
                "traffic_bps": 2_700_000,
                "traffic_pps": 1480,
                "active_flows": 23,
                "issues": ["RTT 偏高（210ms）", "TCP 丢包率 3.2%"],
            }
        },
        "list_interfaces": {"interfaces": ["eth0", "wlan0"]},
        "get_flows": {
            "flow_count": 4,
            "returned": 3,
            "interval_seconds": 1,
            "interface_filter": "eth0",
            "top_by_bps": [
                {
                    "src": "10.0.0.5", "dst": "223.5.5.5", "sport": 51422, "dport": 443,
                    "protocol": "TCP", "bps": 2_700_000, "pps": 1480, "pid": 8891,
                },
                {
                    "src": "10.0.0.5", "dst": "140.82.114.4", "sport": 51430, "dport": 443,
                    "protocol": "TCP", "bps": 41_000, "pps": 32, "pid": 3120,
                },
            ],
            "top_by_pps": [
                {
                    "src": "10.0.0.5", "dst": "223.5.5.5", "sport": 51422, "dport": 443,
                    "protocol": "TCP", "bps": 2_700_000, "pps": 1480, "pid": 8891,
                },
            ],
        },
        "search_network_history": {
            "query": "eth0 RTT 升高和 TCP 丢包的历史记录与排查方式",
            "count": 3,
            "results": [
                {
                    "chunk_id": "log_c2338151",
                    "text": "10:01:30 网络质量变化: FAIR (分数 64.0, 接口 eth0)；"
                            "10:01:58 RTT_MONITOR: eth0 RTT 210ms",
                    "time_window": "10:01:30~10:02:00",
                    "interface": "eth0",
                },
                {
                    "chunk_id": "kb_high_rtt",
                    "text": "RTT 超过 100ms 属于严重延迟。常见原因：网络拥塞、"
                            "路由抖动、DNS 解析慢、链路降速。排查顺序建议先本地后远端。",
                    "topic": "延迟过高排查",
                },
            ],
        },
    }

    def run(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        if name == "ping_host":
            return {
                "ok": True,
                "host": args.get("hostname", ""),
                "interface": "eth0",
                "rtt_ms": 205,
            }
        if name not in self.FAKE:
            return {"error": f"演示模式没有为 {name} 准备假数据"}
        # 深拷一层，避免调用方改动污染类属性（假数据是共享的）
        import copy

        return copy.deepcopy(self.FAKE[name])


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()

    runner = DemoToolRunner()
    print(f"演示脚本轮数：{len(default_demo_script())}")
    print(f"可提供假数据的工具：{sorted(DemoToolRunner.FAKE) + ['ping_host']}")
    print()
    print("跑一次完整轨迹：python main.py --dry-run")
    print()
    print(
        "（本模块叫 offline_demo 而不是 demo：rag/ 目录里已经有一个 demo.py，"
        "而 rag/ 在 sys.path 上，重名会被它盖掉。）"
    )
