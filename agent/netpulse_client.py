"""
NetPulse D-Bus 客户端 —— Python 访问 C++ 服务的唯一入口。

架构::

    C++ server (eBPF 采集 / 多线程监控)
        │
        │ D-Bus (SESSION bus)
        ▼
    netpulse_client.py  ←── 本模块
        │
        ▼
    tools.py → LLM Function Calling → Agent 循环

服务端 D-Bus 坐标（定义见 server/include/common.hpp）::

    bus name    : com.example.WeakNet
    object path : /com/example/WeakNet
    interface   : com.example.WeakNet
    bus type    : SESSION

可用方法（见 server/src/dbus_service.cpp）::

    GetInterfaces() -> as          网卡名列表
    ListInterfaces() -> as         同上（别名）
    HealthCheck()   -> s           JSON 字符串，含全套指标
    Ping(host: s)   -> s           主动探测，返回文本结果
    GetFlows()      -> s           连接明细（五元组 + 速率），JSON 字符串
    Get()           -> s           示例方法

可用信号::

    NetworkQualityChanged(quality: s, details: s, counter: i)
    InterfaceChanged(message: s, counter: i)
    ConnectionModeChanged(message: s, counter: i)
    Changed(message: s, counter: i)
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

# 惰性导入 dbus。
#
# dbus-python 是 Linux 专有的（Ubuntu 上 `sudo apt install python3-dbus`），
# 在 Windows/macOS 上根本装不上。而本模块被 tools.py 导入，tools.py 又被
# Agent 循环导入 —— 如果这里硬 import，整个 Agent 连"跑离线测试"都做不到。
#
# 这条链路上真正依赖 dbus 的只有 D-Bus 调用本身，其它（消息翻译、循环、
# 容错、检索工具）全是纯 Python。依赖应该按这个边界切开。
try:
    import dbus

    _DBUS_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover
    dbus = None  # type: ignore[assignment]
    _DBUS_IMPORT_ERROR = exc

# 给 except 子句用。dbus 缺席时是空元组 —— `except ():` 合法且什么都不捕获。
_DBUS_EXCEPTIONS: tuple[type[BaseException], ...] = (
    (dbus.exceptions.DBusException,) if dbus is not None else ()
)

BUS_NAME = "com.example.WeakNet"
OBJECT_PATH = "/com/example/WeakNet"
INTERFACE = "com.example.WeakNet"

SIGNAL_CHANGED = "Changed"
SIGNAL_INTERFACE_CHANGED = "InterfaceChanged"
SIGNAL_CONNECTION_MODE_CHANGED = "ConnectionModeChanged"
SIGNAL_NETWORK_QUALITY_CHANGED = "NetworkQualityChanged"

# GetFlows 是后加的方法。老版本的服务端没有它，调用会收到 UnknownMethod ——
# 见 get_flows() 里对这种情况的处理：不能让它变成一句看不懂的 D-Bus 报错。
METHOD_GET_FLOWS = "GetFlows"

# 服务端 handlePing 的两种返回格式（server/src/dbus_service.cpp）
_PING_OK = re.compile(r"^PING\s+(?P<host>\S+)\s+via\s+(?P<iface>\S+):\s+(?P<rtt>\d+)ms$")
_PING_FAIL = re.compile(
    r"^PING\s+(?P<host>\S+)\s+via\s+(?P<iface>\S+):\s+FAILED\s+\(error code:\s*(?P<code>-?\d+)\)$"
)

# 服务端用来表示"该字段不适用"的哨兵值（见 server/include/net_info.hpp）
RSSI_NOT_WIFI = -1000
LOSS_UNKNOWN = -1.0
RTT_UNKNOWN = -1


class NetPulseError(RuntimeError):
    """服务不可用，或调用/解析失败。"""


class NetPulseClient:
    """与 NetPulse C++ 服务通信的 D-Bus 客户端。

    用法::

        client = NetPulseClient()
        client.list_interfaces()    # ['wlan0', 'eth0']
        client.health_check()       # {'interface': 'wlan0', 'rtt_ms': 12, ...}
        client.ping("8.8.8.8")      # {'ok': True, 'rtt_ms': 23, ...}
        client.get_flows()          # {'flows': [{'src': ..., 'bps': ...}, ...]}
    """

    def __init__(self, bus: "dbus.Bus | None" = None) -> None:
        if dbus is None:
            raise NetPulseError(_no_dbus_help())
        self._bus = bus or dbus.SessionBus()
        try:
            obj = self._bus.get_object(BUS_NAME, OBJECT_PATH)
        except _DBUS_EXCEPTIONS as exc:
            raise NetPulseError(_connection_help(exc)) from exc
        self._iface = dbus.Interface(obj, INTERFACE)

    # ------------------------------------------------------------------
    # D-Bus 方法
    # ------------------------------------------------------------------

    def list_interfaces(self) -> list[str]:
        """列出所有网卡名。服务端保证非空（无网卡时兜底返回 ['eth0']）。"""
        return [str(name) for name in self._iface.GetInterfaces()]

    def health_check_raw(self) -> str:
        """HealthCheck 的原始 JSON 字符串。"""
        return str(self._iface.HealthCheck())

    def health_check(self) -> dict[str, Any]:
        """解析后的健康检查结果。

        字段来自 server/src/network_quality_assessor.cpp 的 generateMetricsJson::

            interface       网卡名
            quality_score   0-100 健康分
            rtt_ms          往返延迟（毫秒）
            tcp_loss_rate   TCP 丢包率（百分比）
            rssi_dbm        WiFi 信号强度，非 WiFi 网卡为 -1000
            traffic_bps     当前带宽（bytes/s）
            traffic_pps     当前包速率（packets/s）
            active_flows    活跃连接数
            quality_level   0=未知 1=差 2=一般 3=良好 4=优秀
            using_now       是否当前上网网卡
            issues          检出的问题描述列表
        """
        raw = self.health_check_raw()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NetPulseError(f"HealthCheck 未返回合法 JSON: {raw!r}") from exc

    def ping(self, hostname: str) -> dict[str, Any]:
        """Ping 指定主机（服务端自动选择当前上网网卡，3 秒超时）。

        返回::

            成功 {'ok': True,  'host': ..., 'interface': ..., 'rtt_ms': 23, 'raw': ...}
            失败 {'ok': False, 'host': ..., 'interface': ..., 'error_code': -1, 'raw': ...}
        """
        raw = str(self._iface.Ping(hostname))
        if m := _PING_OK.match(raw):
            return {
                "ok": True,
                "host": m["host"],
                "interface": m["iface"],
                "rtt_ms": int(m["rtt"]),
                "raw": raw,
            }
        if m := _PING_FAIL.match(raw):
            return {
                "ok": False,
                "host": m["host"],
                "interface": m["iface"],
                "error_code": int(m["code"]),
                "raw": raw,
            }
        # 服务端格式若有变化，不至于抛异常
        return {"ok": False, "host": hostname, "raw": raw, "error": "无法解析服务端返回"}

    def get_flows(self) -> dict[str, Any]:
        """连接明细（五元组 + 速率）。

        服务端返回的 JSON 结构::

            {"interface": "eth0",
             "interval_seconds": 1,
             "flows": [{"src": "...", "dst": "...", "sport": 1234, "dport": 443,
                        "protocol": "TCP", "bps": 20480, "pps": 34, "pid": 8891}]}

        ⚠️ 三个必须知道的行为（都来自底层 eBPF 采样方式，不是 bug）：

        1. **这会阻塞约 `interval_seconds` 秒。**
           速率 = 两次采样的差值 ÷ 时间，所以服务端必须真的等一个窗口。
           诊断工具调一次等 1 秒可以接受，但别在紧循环里调。

        2. **bps / pps 是速率，不是累计量。** 单位分别是 bytes/s 和 packets/s。

        3. **没有流量的连接不会出现。** 服务端会丢掉窗口内增量为 0 的条目，
           所以"连接数比 get_conn_stats 里的 active_flows 少"是正常的 ——
           这里是**正在传数据的连接**，那里是**已建立的连接**。
        """
        try:
            raw = str(self._iface.GetFlows())
        except _DBUS_EXCEPTIONS as exc:
            raise NetPulseError(_get_flows_help(exc)) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NetPulseError(f"GetFlows 未返回合法 JSON: {raw!r}") from exc

        if not isinstance(data, dict):
            raise NetPulseError(f"GetFlows 返回结构不认识（期望 object）：{raw!r}")

        data.setdefault("flows", [])
        return data

    # ------------------------------------------------------------------
    # 信号订阅
    # ------------------------------------------------------------------

    def subscribe_quality(self, handler: Callable[[str, str, int], None]) -> None:
        """订阅网络质量变化。

        handler(quality: str, details_json: str, counter: int)
        """
        self._bus.add_signal_receiver(
            handler,
            signal_name=SIGNAL_NETWORK_QUALITY_CHANGED,
            dbus_interface=INTERFACE,
        )

    def subscribe_event(
        self, signal_name: str, handler: Callable[[str, int], None]
    ) -> None:
        """订阅单参数事件信号（InterfaceChanged / ConnectionModeChanged / Changed）。

        handler(message: str, counter: int)
        """
        self._bus.add_signal_receiver(
            handler,
            signal_name=signal_name,
            dbus_interface=INTERFACE,
        )


# ---------------------------------------------------------------------------
# 错误信息
# ---------------------------------------------------------------------------


def _dbus_error_name(exc: Exception) -> str:
    """从 D-Bus 异常里取出错误名（形如 org.freedesktop.DBus.Error.UnknownMethod）。

    不同版本的 dbus-python 取法不一样，所以逐个试，拿不到就退回空串 ——
    这里绝不能因为"取错误名失败"而把真正的错误盖掉。
    """
    getter = getattr(exc, "get_dbus_name", None)
    if callable(getter):
        try:
            name = getter()
            if name:
                return str(name)
        except Exception:  # noqa: BLE001
            pass
    return str(exc)


def _no_dbus_help() -> str:
    return (
        "没有安装 dbus-python，无法访问 NetPulse 服务。\n"
        "dbus-python 是 **Linux 专有**的，Windows / macOS 上装不了。\n"
        "（这本来就是个 Linux 项目 —— eBPF 只在 Linux 上跑。）\n"
        "\n"
        "在 WSL 或 Ubuntu 上：\n"
        "    sudo apt install python3-dbus\n"
        "\n"
        "如果你只是想在 Windows 上跑 Agent 的离线测试，不需要装它：\n"
        "    python test_agent.py        # 用假模型，不碰 D-Bus\n"
        "    python main.py --dry-run    # 看完整推理轨迹"
    )


def _get_flows_help(exc: Exception) -> str:
    """GetFlows 调用失败时的提示。

    最常见的失败不是"服务没起"，而是**服务是旧版本**，还没编译进 GetFlows。
    这两种情况的提示应该完全不同，否则用户会去查错方向。
    """
    name = _dbus_error_name(exc)
    if "UnknownMethod" in name or "unknown method" in str(exc).lower():
        return (
            "服务端没有 GetFlows 方法 —— 大概率是 C++ 服务还没重新编译。\n"
            "修法：\n"
            "    1) 确认 server/include/common.hpp 里有 kMethodGetFlows\n"
            "    2) 确认 server/src/dbus_service.cpp 里注册了它的分发分支\n"
            "    3) 重新编译并重启服务：\n"
            "         make            # 项目根目录\n"
            "         sudo -E ./server/bin/weaknet-dbus-server\n"
            "    （改动清单见 agent/README.md 的『GetFlows 需要改的 C++』一节）\n"
            f"\n原始错误：{exc}"
        )
    return _connection_help(exc)


def _connection_help(exc: Exception) -> str:
    return (
        f"连不上 NetPulse 服务（{BUS_NAME}）。\n"
        "逐条排查：\n"
        "  1) server 起了吗？   ./server/bin/weaknet-dbus-server\n"
        "  2) server 和本脚本在同一个 D-Bus session 上吗？\n"
        "     server 需要 root 权限读 eBPF，但 sudo 默认会丢掉\n"
        "     DBUS_SESSION_BUS_ADDRESS，导致 server 连到 root 自己的\n"
        "     总线上去，Python 这边就看不到它。用：\n"
        "         sudo -E ./server/bin/weaknet-dbus-server\n"
        "     或者显式传：\n"
        "         sudo DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS \\\n"
        "              ./server/bin/weaknet-dbus-server\n"
        "  3) 验证服务是否注册成功：\n"
        "         dbus-send --session --print-reply --dest=com.example.WeakNet \\\n"
        "             /com/example/WeakNet com.example.WeakNet.GetInterfaces\n"
        f"\n原始错误：{exc}"
    )
