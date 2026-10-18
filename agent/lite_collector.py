"""
LiteCollector —— 纯 Python 的指标采集器（不依赖 C++ 服务端 / D-Bus / eBPF）。

===========================================================================
它解决什么问题
===========================================================================

完整模式的数据链路是::

    web/agent ──D-Bus──► weaknet-dbus-server（C++，eBPF 采集）

这条链路要求：Linux 内核（eBPF 是内核特性，打包不掉）、编译好的二进制、
一条 session 总线。一台"干净的电脑"（Windows/macOS、没装 WSL）永远接不上。

LiteCollector 把数据源换成::

    psutil（网卡/连接/计数器） + 系统 ping（RTT/丢包）
    + host_bridge（Windows 上查宿主 WiFi，纯标准库 + PowerShell）

于是同一条工具链（tools.py → Agent → LLM）在**任何平台**都能跑，
代价是拿不到 eBPF 的连接级流量明细（get_flows 的 bps/pps 为 0）。

这不是"降级凑合"，而是有意的第三条数据通路::

    完整模式   eBPF + D-Bus      生产用，Linux only
    轻量模式   psutil + ping     跨平台演示/日常用，pip install 即用
    演示模式   假模型 + 假工具    零依赖看界面

===========================================================================
怎么启用
===========================================================================

    · 显式：环境变量 NETPULSE_LITE=1（start.py --lite 会自动设）
    · 自动：平台上根本没有 dbus-python（Windows/macOS）且有 psutil 时，
      ToolRunner 自动切过来 —— 否则每个工具都只会报"D-Bus 是 Linux 专有的"

接口与 NetPulseClient 对齐（鸭子类型），tools.py 无感::

    list_interfaces() -> list[str]
    health_check()   -> dict    字段与 HealthCheck 的 JSON 一致
    ping(host)       -> dict    {'ok':..., 'rtt_ms':...}
    get_flows()      -> dict    {'flows': [...], ...}
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import time
from typing import Any

# psutil 惰性导入：没装它的时候，本模块本身还能被 import（比如被 --help
# 或文档工具扫到），只是真用起来才报"缺 psutil"——和 netpulse_client
# 对 dbus 的处理同一个思路。
try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

# host_bridge 是纯标准库 + PowerShell interop，Windows 原生就能用
# （is_wsl() 只为判断"是不是 WSL"，查宿主 WiFi 本身不依赖 WSL）。
try:
    import host_bridge
except ImportError:  # pragma: no cover
    host_bridge = None  # type: ignore[assignment]

PING_TARGET = "223.5.5.5"  # 阿里云公共 DNS，和服务端 RTT 监控同一个目标

# 与 NetPulseClient 相同的哨兵值（见 netpulse_client.py / net_info.hpp）
RSSI_NOT_WIFI = -1000
LOSS_UNKNOWN = -1.0
RTT_UNKNOWN = -1

# 两次采样算速率用的模块级状态（与 host_bridge._RATE_STATE 同思路）。
# 单线程 Web 服务里够用；真要并发采样得加锁，这里不引入那个复杂度。
_RATE_STATE: dict[str, Any] = {"rx": 0, "tx": 0, "rp": 0, "tp": 0, "t": 0.0}


class LiteCollectorError(RuntimeError):
    """轻量模式自己特有的错误（缺 psutil 等）。"""


def _powershell_available() -> bool:
    """宿主机桥接要靠 PowerShell：WSL interop（powershell.exe）或原生 Windows。"""
    return any(
        shutil.which(name)
        for name in ("powershell.exe", "pwsh.exe", "powershell", "pwsh")
    )


def _need_psutil() -> Any:
    if psutil is None:
        raise LiteCollectorError(
            "轻量模式需要 psutil（pip install psutil）。\n"
            "  或者改用完整模式：在 WSL/Linux 里 python start.py。"
        )
    return psutil


# ---------------------------------------------------------------------------
# 网卡枚举
# ---------------------------------------------------------------------------


def _is_loopback(name: str, addrs: Any) -> bool:
    if name == "lo":
        return True
    # Windows 上没有 lo，但有 Loopback 伪接口；按 AF_INET 的 127.0.0.1 判断
    for addr in addrs:
        if getattr(addr, "family", None) == getattr(getattr(psutil, "AF_INET", 2), "value", 2):
            if str(getattr(addr, "address", "")).startswith("127."):
                return True
    return False


def list_interfaces() -> list[str]:
    """列出非回环网卡名。与 NetPulseClient.list_interfaces 同语义。"""
    ps = _need_psutil()
    names: list[str] = []
    try:
        addrs = ps.net_if_addrs()
        stats = ps.net_if_stats()
    except Exception:
        return ["eth0"]
    for name, addr_list in addrs.items():
        if _is_loopback(name, addr_list):
            continue
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        names.append(name)
    # 服务端保证非空；这里同样兜底
    return names or ["eth0"]


def _default_iface() -> str:
    """当前上网网卡。Linux 读默认路由，其它平台取第一块活动的非回环网卡。"""
    names = list_interfaces()
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/net/route", "r", encoding="ascii", errors="replace") as fh:
                next(fh)  # 表头
                for line in fh:
                    parts = line.split()
                    # 目标地址全 0 的那行就是默认路由
                    if len(parts) > 1 and parts[1] == "00000000":
                        return parts[0]
        except OSError:
            pass
    return names[0]


# ---------------------------------------------------------------------------
# ping（跨平台）：Windows 用 -n/-w，类 Unix 用 -c/-W
# ---------------------------------------------------------------------------

# Windows 中文版输出："平均 = 19ms" / "丢失 = 0 (0% 丢失)"
# Windows 英文版输出："Average = 19ms" / "Lost = 0 (0% loss)"
# Linux 输出：      "rtt min/avg/max/mdev = 19.0/19.5/..." / "0% packet loss"
_RTT_CN = re.compile(r"平均\s*=\s*(\d+(?:\.\d+)?)\s*ms")
_RTT_EN = re.compile(r"Average\s*=\s*(\d+(?:\.\d+)?)\s*ms")
_RTT_LINUX = re.compile(r"=\s*[\d.]+/([\d.]+)/")
_LOSS_CN = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*丢失")
_LOSS_EN = re.compile(r"\((\d+(?:\.\d+)?)%\s*loss")
_LOSS_LINUX = re.compile(r"(\d+(?:\.\d+)?)%\s*packet loss")


def _decode_output(raw: bytes) -> str:
    """把命令输出的字节解码成文本。

    为什么不能直接 ``text=True``：Windows 的 ping 按**控制台代码页**输出
    （中文系统是 GBK/cp936），而 Python 的 text=True 按 locale 或 UTF-8 解码，
    两边对不上就抛 UnicodeDecodeError —— 而且是在 subprocess 的读取线程里
    抛的，栈trace 指向 subprocess.py，看不出是编码问题（实测踩中）。
    数字和关键词全是 ASCII，所以解码方案按"能解出中文最好，解不出也别炸"排。
    """
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")  # pragma: no cover


def _ping_raw(host: str, count: int = 3, timeout_ms: int = 3000) -> tuple[bool, str]:
    """跑一次系统 ping。返回 (是否成功执行, 解码后的原始输出)。

    中英文系统、Windows/Linux/macOS 的输出格式全都不同，所以这里只负责
    把原始文本拿回来，解析交给 _parse_ping。
    """
    is_windows = sys.platform.startswith("win")
    if is_windows:
        cmd = ["ping", "-n", str(count), "-w", str(timeout_ms), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(max(1, timeout_ms // 1000)), host]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout_ms / 1000 + count + 5
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ping 执行失败：{exc}"
    # Windows 上 ping 不通也返回 0，成败看输出；Linux 不通返回非 0
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    return True, _decode_output(raw)


def _parse_ping(raw: str) -> tuple[int | None, float | None]:
    """从 ping 输出里抠出 (平均 RTT ms, 丢包率 %)。抠不到就是 (None, None)。"""
    rtt: int | None = None
    loss: float | None = None
    for pattern in (_RTT_CN, _RTT_EN, _RTT_LINUX):
        m = pattern.search(raw)
        if m:
            rtt = int(round(float(m.group(1))))
            break
    for pattern in (_LOSS_CN, _LOSS_EN, _LOSS_LINUX):
        m = pattern.search(raw)
        if m:
            loss = float(m.group(1))
            break
    return rtt, loss


def ping(hostname: str) -> dict[str, Any]:
    """Ping 指定主机。返回结构与 NetPulseClient.ping 一致。"""
    iface = _default_iface()
    ok, raw = _ping_raw(hostname or PING_TARGET)
    if not ok:
        return {"ok": False, "host": hostname, "interface": iface, "raw": raw,
                "error_code": -1}
    rtt, loss = _parse_ping(raw)
    if rtt is None:
        return {"ok": False, "host": hostname, "interface": iface, "raw": raw,
                "error_code": -1}
    return {"ok": True, "host": hostname, "interface": iface, "rtt_ms": rtt,
            "raw": raw, "loss_pct": loss}


# ---------------------------------------------------------------------------
# 速率（两次采样求差，语义与 host_bridge / C++ 端一致）
# ---------------------------------------------------------------------------


def _sample_rates(iface: str) -> tuple[float, float, str]:
    """返回 (下载 B/s, 上传 B/s, rate_basis)。

    rate_basis 的两种取值和 host_bridge._apply_rates 完全一致：
    byte_delta = 有基准样本，差值是真的；no_baseline = 第一次调用，
    算不出速率（不是"网络没流量"）。
    """
    ps = _need_psutil()
    try:
        counters = ps.net_io_counters(pernic=True).get(iface)
    except Exception:
        counters = None
    if counters is None:
        return 0.0, 0.0, "no_baseline"
    now = time.monotonic()
    rx, tx = int(counters.bytes_recv), int(counters.bytes_sent)
    rp, tp = int(counters.packets_recv), int(counters.packets_sent)
    dt = now - _RATE_STATE["t"]
    if _RATE_STATE["t"] > 0 and dt > 0.1:
        dl = (rx - _RATE_STATE["rx"]) / dt if rx >= _RATE_STATE["rx"] else 0.0
        ul = (tx - _RATE_STATE["tx"]) / dt if tx >= _RATE_STATE["tx"] else 0.0
        _RATE_STATE.update(rx=rx, tx=tx, rp=rp, tp=tp, t=now)
        return round(dl, 1), round(ul, 1), "byte_delta"
    _RATE_STATE.update(rx=rx, tx=tx, rp=rp, tp=tp, t=now)
    return 0.0, 0.0, "no_baseline"


# ---------------------------------------------------------------------------
# 健康检查（字段与 HealthCheck JSON 对齐，见 netpulse_client.health_check）
# ---------------------------------------------------------------------------


def _score(rtt: int | None, loss: float | None) -> tuple[float, int, list[str]]:
    """按 RTT/丢包给一个 0-100 的分和问题清单。

    这是**简化启发式**，和 C++ 端 NetworkQualityAssessor 的多维评估不是
    一套标准——轻量模式的定位是"链路通不通、大致好不好"，不是精确评分。
    """
    score = 100.0
    issues: list[str] = []
    if rtt is None:
        score -= 10
        issues.append("RTT 测量失败（ping 未返回延迟）")
    elif rtt > 150:
        score -= 25
        issues.append(f"RTT 偏高（{rtt}ms）")
    elif rtt > 80:
        score -= 10
        issues.append(f"RTT 略高（{rtt}ms）")
    if loss is None:
        pass  # 拿不到丢包率不扣分：ping 输出格式因系统而异，误报比漏报糟
    elif loss > 5:
        score -= 40
        issues.append(f"丢包严重（{loss}%）")
    elif loss > 1:
        score -= 15
        issues.append(f"存在丢包（{loss}%）")
    score = max(0.0, min(100.0, score))
    if rtt is None and loss is None:
        level = 0
    elif score >= 90:
        level = 4
    elif score >= 75:
        level = 3
    elif score >= 50:
        level = 2
    else:
        level = 1
    return round(score, 1), level, issues


def health_check() -> dict[str, Any]:
    """采集一次健康检查。字段与 C++ 服务端的 HealthCheck JSON 对齐。"""
    ps = _need_psutil()
    iface = _default_iface()

    ok, raw = _ping_raw(PING_TARGET)
    rtt, loss = _parse_ping(raw) if ok else (None, None)

    dl, ul, basis = _sample_rates(iface)
    try:
        rp_tp = ps.net_io_counters(pernic=True).get(iface)
        pps = 0.0
        if rp_tp is not None and _RATE_STATE["t"] > 0:
            pps = round(
                ((rp_tp.packets_recv - _RATE_STATE["rp"]) + (rp_tp.packets_sent - _RATE_STATE["tp"]))
                / max(time.monotonic() - _RATE_STATE["t"], 0.1),
                1,
            )
    except Exception:
        pps = 0.0
    try:
        active_flows = len(ps.net_connections(kind="inet"))
    except Exception:
        # 某些系统上列别人进程的连接要权限；拿不到就报 0 并说明
        active_flows = 0

    score, level, issues = _score(rtt, loss)

    data: dict[str, Any] = {
        "interface": iface,
        "using_now": True,
        "quality_score": score,
        "quality_level": level,
        "rtt_ms": rtt if rtt is not None else RTT_UNKNOWN,
        "tcp_loss_rate": loss if loss is not None else LOSS_UNKNOWN,
        "rssi_dbm": RSSI_NOT_WIFI,  # psutil 拿不到信号强度；不是 WiFi 网卡
        "traffic_bps": round(dl + ul, 1),
        "traffic_pps": pps,
        "active_flows": active_flows,
        "issues": issues,
        # 供上层区分数据通路（eBPF 服务端没有这个字段）
        "data_source": "lite_psutil",
        "platform": platform.platform(),
    }

    # wifi 块：measure_network_performance / get_network_health 的 real_uplink
    # 读的就是它。三条来路，按优先级：
    #   1. host_bridge（WSL interop 或原生 Windows 的 powershell.exe）——
    #      真实宿主 WiFi：SSID/信号/信道/抖动都有，和完整模式同一份数据
    #   2. 本地测量合成（纯 Linux 没 PowerShell）—— 无 SSID/信号，但四项指标有值
    #   3. 两块都失败 → 没有 wifi 块，上层照常工作（只是没有 real_uplink）
    wifi: dict[str, Any] | None = None
    if host_bridge is not None and _powershell_available():
        try:
            got = host_bridge.query_host_wifi(ping_target=PING_TARGET)
        except Exception:
            got = None
        if got and got.get("ok"):
            wifi = got
    if wifi is None and (rtt is not None or dl or ul):
        wifi = {
            "interface": iface,
            "rtt_ms": rtt,
            "tcp_loss_pct": loss,
            "download_bps": dl,
            "upload_bps": ul,
            "rate_basis": basis,
            "note": (
                "轻量模式（psutil）：无 SSID/信号强度/抖动——"
                "psutil 拿不到这些；速率是网卡计数器差值（实时流量，非宽带能力）。"
            ),
        }
    if wifi is not None:
        data["wifi"] = wifi
    return data


# ---------------------------------------------------------------------------
# 连接明细（无 eBPF：有五元组和 PID，没有 per-flow 速率）
# ---------------------------------------------------------------------------


def get_flows() -> dict[str, Any]:
    """当前连接列表。bps/pps 恒为 0 —— 轻量模式没有 eBPF，拿不到 per-flow 速率。

    五元组 + PID 来自 psutil.net_connections，本身仍有价值
    （"谁在连什么"），速率那一列需要完整模式的 eBPF 采样。
    """
    ps = _need_psutil()
    iface = _default_iface()
    flows: list[dict[str, Any]] = []
    try:
        conns = ps.net_connections(kind="inet")
    except Exception as exc:
        return {
            "interface": iface,
            "interval_seconds": 1,
            "flows": [],
            "note": f"列连接失败（可能要权限）：{exc}",
        }
    for c in conns:
        if not c.raddr:
            continue  # 只保留有远端的（LISTEN 之类的没价值）
        flows.append(
            {
                "src": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                "dst": f"{c.raddr.ip}:{c.raddr.port}",
                "sport": c.laddr.port if c.laddr else 0,
                "dport": c.raddr.port,
                "protocol": "TCP" if c.type == socket_type_tcp() else "UDP",
                "bps": 0,
                "pps": 0,
                "pid": c.pid or 0,
                "status": c.status,
            }
        )
    return {
        "interface": iface,
        "interval_seconds": 1,
        "flows": flows,
        "note": (
            "轻量模式（psutil）：有五元组与 PID，**没有 per-flow 速率**——"
            "速率需要完整模式的 eBPF 采样，bps/pps 恒为 0。"
        ),
    }


def socket_type_tcp() -> Any:
    """psutil.SOCK_STREAM 的取值；拿不到就退 1（Linux 上就是 1）。"""
    ps = _need_psutil()
    return getattr(ps, "SOCK_STREAM", 1)


class LiteCollector:
    """ Duck-typed NetPulseClient 替身。方法签名与返回值对齐，见模块 docstring。"""

    def __init__(self) -> None:
        _need_psutil()

    def list_interfaces(self) -> list[str]:
        return list_interfaces()

    def health_check(self) -> dict[str, Any]:
        return health_check()

    def ping(self, hostname: str) -> dict[str, Any]:
        return ping(hostname)

    def get_flows(self) -> dict[str, Any]:
        return get_flows()
