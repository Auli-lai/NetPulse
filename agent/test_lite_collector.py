"""lite_collector 的离线测试 —— 全部 mock，不需要 psutil 真机、不需要真的 ping。

跑法：
    python test_lite_collector.py

覆盖：
    · 网卡枚举排除回环、默认路由解析
    · ping 输出解析（Windows 中文 / Windows 英文 / Linux 三种格式）
    · health_check 字段契约（与 C++ HealthCheck JSON 对齐的部分）
    · wifi 块三条来路（桥接 / 合成 / 没有）
    · 速率采样的 rate_basis 语义（首采样 no_baseline，再次 byte_delta）
    · 缺 psutil 时的报错文案
"""

from __future__ import annotations

import importlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")
        if detail:
            print(f"       {detail[:400]}")


# ---------------------------------------------------------------------------
# 假 psutil
# ---------------------------------------------------------------------------


def _make_fake_psutil() -> types.ModuleType:
    mod = types.ModuleType("psutil")

    class _Addr:
        def __init__(self, family: int, address: str) -> None:
            self.family = family
            self.address = address

    class _Stat:
        def __init__(self, isup: bool = True) -> None:
            self.isup = isup

    class _IOCounters:
        def __init__(self, rx: int, tx: int, rp: int, tp: int) -> None:
            self.bytes_recv = rx
            self.bytes_sent = tx
            self.packets_recv = rp
            self.packets_sent = tp

    AF_INET = 2
    mod.AF_INET = AF_INET
    mod.SOCK_STREAM = 1
    mod.net_if_addrs = lambda: {
        "lo": [_Addr(AF_INET, "127.0.0.1")],
        "eth0": [_Addr(AF_INET, "172.30.1.5")],
        "wlan0": [_Addr(AF_INET, "192.168.31.7")],
    }
    mod.net_if_stats = lambda: {
        "lo": _Stat(True),
        "eth0": _Stat(True),
        "wlan0": _Stat(True),
    }
    mod._counters = {"eth0": _IOCounters(1000, 500, 10, 5)}
    mod.net_io_counters = lambda pernic=False: (
        {"eth0": mod._counters["eth0"]} if pernic else mod._counters["eth0"]
    )
    mod.net_connections = lambda kind="inet": []
    return mod


def _fresh_module(fake_psutil: types.ModuleType | None = None) -> types.ModuleType:
    """重新加载 lite_collector，可按需注入假 psutil。"""
    sys.modules.pop("lite_collector", None)
    if fake_psutil is not None:
        sys.modules["psutil"] = fake_psutil
    else:
        sys.modules.pop("psutil", None)
    return importlib.import_module("lite_collector")


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_list_interfaces() -> None:
    print("\n[1] list_interfaces：排除回环、跳过down网卡")
    lite = _fresh_module(_make_fake_psutil())
    names = lite.list_interfaces()
    check("不含 lo", "lo" not in names, str(names))
    check("含 eth0 和 wlan0", {"eth0", "wlan0"} <= set(names), str(names))
    check("顺序稳定", names == ["eth0", "wlan0"], str(names))


def test_default_iface_from_route() -> None:
    print("\n[2] _default_iface：Linux 读默认路由")
    lite = _fresh_module(_make_fake_psutil())
    route = (
        "Iface\tDestination\tGateway\n"
        "eth0\t00000000\t010011AC\n"
        "wlan0\t000011AC\t00000000\n"
    )
    import builtins

    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if str(file) == "/proc/net/route":
            import io

            return io.StringIO(route)
        return real_open(file, *args, **kwargs)

    builtins.open = fake_open
    try:
        iface = lite._default_iface()
    finally:
        builtins.open = real_open
    check("默认路由网卡是 eth0", iface == "eth0", iface)


def test_ping_parsing() -> None:
    print("\n[3] ping 解析：三种系统输出格式")
    lite = _fresh_module(_make_fake_psutil())

    class _Proc:
        def __init__(self, out: str) -> None:
            self.stdout = out.encode("utf-8")
            self.stderr = b""

    cases = [
        (
            "Windows 中文",
            "正在 Ping 223.5.5.5 具有 32 字节的数据:\n"
            "来自 223.5.5.5 的回复: 字节=32 时间=19ms TTL=52\n"
            "来自 223.5.5.5 的回复: 字节=32 时间=18ms TTL=52\n"
            "来自 223.5.5.5 的回复: 字节=32 时间=19ms TTL=52\n"
            "\n223.5.5.5 的 Ping 统计信息:\n"
            "    数据包: 已发送 = 3，已接收 = 3，丢失 = 0 (0% 丢失)，\n"
            "往返行程的估计时间(以毫秒为单位):\n"
            "    最短 = 18ms，最长 = 19ms，平均 = 19ms\n",
            19, 0.0,
        ),
        (
            "Windows 英文",
            "Pinging 223.5.5.5 with 32 bytes of data:\n"
            "Reply from 223.5.5.5: bytes=32 time=21ms TTL=52\n"
            "Reply from 223.5.5.5: bytes=32 time=20ms TTL=52\n"
            "Reply from 223.5.5.5: bytes=32 time=22ms TTL=52\n"
            "\nPing statistics for 223.5.5.5:\n"
            "    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),\n"
            "Approximate round trip times in milli-seconds:\n"
            "    Minimum = 20ms, Maximum = 22ms, Average = 21ms\n",
            21, 0.0,
        ),
        (
            "Linux",
            "PING 223.5.5.5 (223.5.5.5) 56(84) bytes of data.\n"
            "64 bytes from 223.5.5.5: icmp_seq=1 ttl=52 time=19.0 ms\n"
            "64 bytes from 223.5.5.5: icmp_seq=2 ttl=52 time=19.4 ms\n"
            "64 bytes from 223.5.5.5: icmp_seq=3 ttl=52 time=19.1 ms\n"
            "\n--- 223.5.5.5 ping statistics ---\n"
            "3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
            "rtt min/avg/max/mdev = 19.0/19.1/19.4/0.1 ms\n",
            19, 0.0,
        ),
        (
            "丢包 33%",
            "PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.\n"
            "--- 10.0.0.1 ping statistics ---\n"
            "3 packets transmitted, 2 received, 33% packet loss, time 2003ms\n"
            "rtt min/avg/max/mdev = 19.0/19.1/19.4/0.1 ms\n",
            19, 33.0,
        ),
    ]
    for name, out, want_rtt, want_loss in cases:
        lite.subprocess.run = lambda cmd, **kw: _Proc(out)  # noqa: B023
        rtt, loss = lite._parse_ping(out)
        check(f"{name}：RTT", rtt == want_rtt, f"got {rtt}")
        check(f"{name}：丢包", loss == want_loss, f"got {loss}")

    # 完全不通：没有 RTT 行
    dead = "PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.\n\n" \
           "--- 10.0.0.1 ping statistics ---\n" \
           "3 packets transmitted, 0 received, 100% packet loss, time 2003ms\n"
    rtt, loss = lite._parse_ping(dead)
    check("完全不通：RTT 为 None", rtt is None, str(rtt))
    check("完全不通：丢包 100%", loss == 100.0, str(loss))
    result = lite.ping("10.0.0.1")
    lite.subprocess.run = lambda cmd, **kw: _Proc(dead)  # noqa: B023
    result = lite.ping("10.0.0.1")
    check("ping() 不通时 ok=False", result.get("ok") is False, str(result))


def test_health_check_contract() -> None:
    print("\n[4] health_check：字段契约与 C++ HealthCheck 对齐")
    lite = _fresh_module(_make_fake_psutil())

    class _Proc:
        stdout = (
            "64 bytes from 223.5.5.5: icmp_seq=1 ttl=52 time=19.0 ms\n"
            "--- 223.5.5.5 ping statistics ---\n"
            "3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
            "rtt min/avg/max/mdev = 19.0/19.2/19.4/0.1 ms\n"
        ).encode("utf-8")
        stderr = b""

    lite.subprocess.run = lambda cmd, **kw: _Proc()  # noqa: B023
    lite._powershell_available = lambda: False  # 纯 Linux：走合成 wifi 块

    data = lite.health_check()
    for key in (
        "interface", "quality_score", "quality_level", "rtt_ms",
        "tcp_loss_rate", "rssi_dbm", "traffic_bps", "traffic_pps",
        "active_flows", "issues", "using_now",
    ):
        check(f"有字段 {key}", key in data, str(sorted(data)))
    check("data_source 标了轻量模式", data.get("data_source") == "lite_psutil", str(data.get("data_source")))
    check("RTT 解析出来", data.get("rtt_ms") == 19, str(data.get("rtt_ms")))
    check("丢包 0", data.get("tcp_loss_rate") == 0.0, str(data.get("tcp_loss_rate")))
    check("非 WiFi 哨兵值", data.get("rssi_dbm") == lite.RSSI_NOT_WIFI, str(data.get("rssi_dbm")))
    check("网络好时分数高", data.get("quality_score", 0) >= 90, str(data.get("quality_score")))
    check("网络好时无 issue", data.get("issues") == [], str(data.get("issues")))
    check("合成了 wifi 块（四项指标有着落）", "wifi" in data, str(data.get("wifi")))
    wifi = data.get("wifi") or {}
    check("wifi 块带 RTT", wifi.get("rtt_ms") == 19, str(wifi))
    check("wifi 块带 rate_basis", wifi.get("rate_basis") in ("byte_delta", "no_baseline"), str(wifi))


def test_health_check_bad_network() -> None:
    print("\n[5] health_check：网络差时扣分并给问题清单")
    lite = _fresh_module(_make_fake_psutil())

    class _Proc:
        stdout = (
            "64 bytes from 223.5.5.5: icmp_seq=1 ttl=52 time=260.0 ms\n"
            "--- 223.5.5.5 ping statistics ---\n"
            "3 packets transmitted, 2 received, 33% packet loss, time 2003ms\n"
            "rtt min/avg/max/mdev = 260.0/260.0/260.0/0.0 ms\n"
        ).encode("utf-8")
        stderr = b""

    lite.subprocess.run = lambda cmd, **kw: _Proc()  # noqa: B023
    lite._powershell_available = lambda: False
    data = lite.health_check()
    check("差网络分数低", data.get("quality_score", 100) < 75, str(data.get("quality_score")))
    check("有问题清单", len(data.get("issues") or []) >= 2, str(data.get("issues")))
    check("提到 RTT 偏高", any("RTT" in i for i in data.get("issues") or []), str(data.get("issues")))
    check("提到丢包", any("丢包" in i for i in data.get("issues") or []), str(data.get("issues")))


def test_rate_basis_semantics() -> None:
    print("\n[6] 速率采样：首采样 no_baseline，再次 byte_delta")
    lite = _fresh_module(_make_fake_psutil())
    lite._powershell_available = lambda: False

    class _Proc:
        stdout = (
            "64 bytes from 223.5.5.5: icmp_seq=1 ttl=52 time=19.0 ms\n"
            "rtt min/avg/max/mdev = 19.0/19.2/19.4/0.1 ms\n"
        ).encode("utf-8")
        stderr = b""

    lite.subprocess.run = lambda cmd, **kw: _Proc()  # noqa: B023

    dl1, ul1, basis1 = lite._sample_rates("eth0")
    check("首采样没有基准", basis1 == "no_baseline", basis1)
    check("首采样速率 0（不是'没流量'）", dl1 == 0.0 and ul1 == 0.0, f"{dl1}/{ul1}")

    # 过 0.2s 后加 2000 字节再采
    import time as _time

    _time.sleep(0.2)
    fake = _make_fake_psutil()
    fake._counters["eth0"].bytes_recv += 2000
    lite.psutil = fake
    dl2, ul2, basis2 = lite._sample_rates("eth0")
    check("二次采样有基准", basis2 == "byte_delta", basis2)
    check("速率按差值算出（量级对）", 5000 < dl2 < 30000, str(dl2))


def test_get_flows_shape() -> None:
    print("\n[7] get_flows：五元组 + PID，速率恒 0 并说明原因")
    lite = _fresh_module(_make_fake_psutil())

    class _Conn:
        def __init__(self) -> None:
            self.laddr = types.SimpleNamespace(ip="172.30.1.5", port=51234)
            self.raddr = types.SimpleNamespace(ip="223.5.5.5", port=443)
            self.type = 1  # SOCK_STREAM
            self.pid = 8891
            self.status = "ESTABLISHED"

    lite.psutil.net_connections = lambda kind="inet": [_Conn()]
    data = lite.get_flows()
    check("有 flows 列表", isinstance(data.get("flows"), list), str(data)[:200])
    check("一条流", len(data["flows"]) == 1, str(data["flows"]))
    flow = data["flows"][0]
    check("五元组 dst", flow.get("dst") == "223.5.5.5:443", str(flow))
    check("协议 TCP", flow.get("protocol") == "TCP", str(flow))
    check("带 PID", flow.get("pid") == 8891, str(flow))
    check("速率恒 0（无 eBPF）", flow.get("bps") == 0 and flow.get("pps") == 0, str(flow))
    check("说明了为什么是 0", "eBPF" in (data.get("note") or ""), str(data.get("note")))


def test_missing_psutil() -> None:
    print("\n[8] 缺 psutil：报错说人话，不抛看不懂的 ImportError")
    lite = _fresh_module(None)
    try:
        lite.list_interfaces()
        check("缺 psutil 时抛错", False, "居然没抛错")
    except lite.LiteCollectorError as exc:
        check("缺 psutil 时抛 LiteCollectorError", True)
        check("错误里带安装命令", "pip install psutil" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("缺 psutil 时抛 LiteCollectorError", False, f"{type(exc).__name__}: {exc}")


def test_tools_picks_lite_without_dbus() -> None:
    print("\n[9] 接线：平台没有 dbus 时 ToolRunner 自动切轻量模式")
    os.environ.pop("NETPULSE_LITE", None)
    lite = _fresh_module(_make_fake_psutil())
    sys.modules["lite_collector"] = lite
    try:
        import tools as tools_mod

        # 模拟 Windows/macOS：dbus 导入失败
        import netpulse_client as npc

        real_dbus = npc.dbus
        npc.dbus = None
        try:
            runner = tools_mod.ToolRunner()
            client = runner.client
            check("自动选了 LiteCollector", isinstance(client, lite.LiteCollector), type(client).__name__)
        finally:
            npc.dbus = real_dbus

        # 显式 NETPULSE_LITE=1 时也走轻量（不管有没有 dbus）
        os.environ["NETPULSE_LITE"] = "1"
        runner2 = tools_mod.ToolRunner()
        check("显式 NETPULSE_LITE=1 走轻量", isinstance(runner2.client, lite.LiteCollector))
    finally:
        os.environ.pop("NETPULSE_LITE", None)
        sys.modules.pop("lite_collector", None)
        sys.modules.pop("tools", None)


def main() -> int:
    print("lite_collector 离线测试（全部 mock，不需要真 psutil / 真 ping）")
    test_list_interfaces()
    test_default_iface_from_route()
    test_ping_parsing()
    test_health_check_contract()
    test_health_check_bad_network()
    test_rate_basis_semantics()
    test_get_flows_shape()
    test_missing_psutil()
    test_tools_picks_lite_without_dbus()
    print(f"\n{'=' * 50}")
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
