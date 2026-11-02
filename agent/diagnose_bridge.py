#!/usr/bin/env python3
"""
NetPulse 桥接诊断脚本（容器内 / 宿主通用）。

只做一件事：验证「宿主 WiFi 桥接」在当前环境下到底通不通，
并把合并结果原样打印出来。不依赖 D-Bus、不依赖 C++ 服务，
所以即使在容器里没有起服务端也能跑。

用法::

    # 容器里（compose 服务名默认 netpulse-full）
    docker exec -i netpulse-full python3 /app/agent/diagnose_bridge.py

    # 宿主原生 WSL / Linux 上
    python3 agent/diagnose_bridge.py

它自己负责把 sys.path 配好，不需要在命令行里手动 insert。
"""

from __future__ import annotations

import os
import sys

# ---- 路径自举：让 netpulse_client / host_bridge 都能 import ----
# 本脚本放在 agent/ 目录下。把 agent/ 本身加入 sys.path 后，
# `import netpulse_client` 和 `import host_bridge` 都会成功。
# 注意：这里故意 **不** 把项目根目录当包来用 `import agent.xxx`，
# 那样在“根目录和 agent 目录同时进 path”时会出现 "agent is not a package"。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import netpulse_client as nc  # noqa: E402


def _main() -> int:
    print("========== NetPulse 桥接诊断 ==========")
    print(f"Python            : {sys.version.split()[0]}")
    print(f"host_bridge None? : {nc.host_bridge is None}")
    print(f"is_wsl()          : {nc.host_bridge.is_wsl() if nc.host_bridge else 'N/A (host_bridge 缺失)'}")
    print(f"_in_container()   : {nc._in_container()}")
    print(f"NETPULSE_IN_CONTAINER = {os.environ.get('NETPULSE_IN_CONTAINER', '(未设置)')}")
    print(f"NETPULSE_BRIDGE_HOST = {os.environ.get('NETPULSE_BRIDGE_HOST', '(未设置)')}")
    print(f"NETPULSE_BRIDGE_URL  = {os.environ.get('NETPULSE_BRIDGE_URL', '(未设置)')}")

    # 直接用模块级函数测合并逻辑（不需要 D-Bus / C++ 服务端）
    merged = nc._merge_host_wifi({"interface": "eth0"})
    wifi = merged.get("wifi") or {}

    print("\n---------- _merge_host_wifi 结果 ----------")
    print(f"host_interface    : {merged.get('host_interface')}")
    print(f"media             : {merged.get('media')}")
    print(f"link_speed        : {merged.get('link_speed')}")
    print(f"uplink_status     : {merged.get('uplink_status')}")
    print(f"wifi.ok           : {wifi.get('ok')}")
    print(f"wifi.interface    : {wifi.get('interface')}")
    print(f"wifi.ssid         : {wifi.get('ssid')}")
    print(f"wifi.rssi_dbm     : {wifi.get('rssi_dbm')}")
    print(f"wifi.signal_pct   : {wifi.get('signal_pct')}")
    print(f"wifi.channel      : {wifi.get('channel')}")
    print(f"wifi_bridge_error : {merged.get('wifi_bridge_error')}")

    print("\n========== 结论 ==========")
    if wifi.get("ok"):
        print(f"✅ 桥接成功：宿主 WiFi = {wifi.get('ssid')} "
              f"({wifi.get('rssi_dbm')} dBm)")
    elif merged.get("wifi_bridge_error"):
        print("❌ 桥接失败，原因见上方 wifi_bridge_error：")
        print(f"   {merged.get('wifi_bridge_error')}")
    else:
        print("⚠️ 未拿到 WiFi 数据（可能是插网线、或 host_bridge 缺失）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
