#!/usr/bin/env python3
"""
宿主桥接守护进程（Docker / 容器场景）—— 让容器里的 NetPulse 看见宿主真实上网网卡。

===========================================================================
为什么需要它
===========================================================================
NetPulse 全栈镜像跑在 Docker 里时，容器有独立网络命名空间，只能看到自己的 eth0；
而 Windows 的 WiFi / 以太网卡在容器里根本不存在。本项目的 WiFi 数据原本靠在 WSL 里
调 ``powershell.exe`` 桥接（见 host_bridge.py），但容器里既没有 powershell.exe、
也执行不了 .exe。

解法：在【宿主】（Windows / WSL，能调 powershell.exe 的环境）起一个轻量 HTTP 服务，
把宿主真实上网网卡 + WiFi 指标查出来，容器通过 ``host.docker.internal`` 来取。

===========================================================================
运行
===========================================================================
    NETPULSE_FORCE_BRIDGE=1 python3 agent/host_bridge_daemon.py
    # 默认监听 0.0.0.0:18900，提供 GET /host-net

start.sh --docker-full 会自动在宿主后台起本进程（常驻，不随终端退出而亡）。

===========================================================================
为什么用「后台线程刷新 + 缓存」而不是每次请求现查
===========================================================================
宿主 WiFi 数据要靠 powershell.exe 现查，而 WSL interop 调 .exe 有时会慢、甚至偶发
卡顿（可达十几秒）。如果每次 HTTP 请求都现查，客户端（容器侧 urlopen(timeout=3)）
会在 powershell 返回前超时断开，服务端还会抛 BrokenPipeError，导致容器永远拿不到
WiFi。

所以这里改成：一个后台线程每 ``_REFRESH_INTERVAL`` 秒刷新一次宿主数据并写入缓存；
HTTP 请求只【瞬间】返回最新缓存。无论 powershell 多慢，客户端都不被阻塞。
首次数据在主线程同步取一次（最多被一次 powershell 超时拖慢），保证端口起来即带数据。

依赖：纯标准库 + 同目录的 host_bridge（同样纯标准库，无第三方包）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# 让同目录的 host_bridge 可被 import（脚本运行时不保证 agent/ 在 sys.path）。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from host_bridge import (  # noqa: E402
    BRIDGE_SOURCE,
    _run_script,
    query_host_wifi,
)

DEFAULT_PORT = int(os.environ.get("NETPULSE_BRIDGE_PORT", "18900"))

# 后台刷新节奏与缓存。
_REFRESH_INTERVAL = 5.0
_LOCK = threading.RLock()
_PAYLOAD: dict[str, Any] = {"ok": False, "error": "尚未初始化", "wifi": {}}


# ---------------------------------------------------------------------------
# 宿主上网网卡探测（默认路由出口，覆盖 WiFi / 以太网 / 其它）
# ---------------------------------------------------------------------------

def _query_uplink() -> dict[str, Any]:
    """找出当前真正上网的那张卡（默认路由出口）及其类型 / 链路速率。

    用 Get-NetRoute 拿默认路由的 InterfaceIndex，再 Get-NetAdapter 取详情。
    MediaType == 'Native 802.11' 判为 WiFi，否则记为 ethernet（含有线 / 虚拟网卡）。
    返回 host_bridge._run_script 的统一契约：成功 {ok:True, ...}，失败 {ok:False, error}。
    """
    script = r'''
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
         Sort-Object RouteMetric | Select-Object -First 1
$idx = if ($route) { $route.InterfaceIndex } else { $null }
$ad = if ($idx) { Get-NetAdapter -InterfaceIndex $idx -ErrorAction SilentlyContinue } else { $null }
if (-not $ad) {
  $ad = Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Up' } | Select-Object -First 1
}
$name = if ($ad) { [string]$ad.Name } else { '' }
$desc = if ($ad) { [string]$ad.InterfaceDescription } else { '' }
$mt   = if ($ad) { [string]$ad.MediaType } else { '' }
$ls   = if ($ad) { [string]$ad.LinkSpeed } else { '' }
$st   = if ($ad) { [string]$ad.Status } else { '' }
$media = if ($mt -eq 'Native 802.11') { 'wifi' } else { 'ethernet' }
$result = [PSCustomObject][ordered]@{
  ok=$true; name=$name; desc=$desc; media_type=$mt; media=$media;
  link_speed=$ls; status=$st
}
$json = $result | ConvertTo-Json -Compress
[Console]::WriteLine('NETPULSE_OK ' + [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($json)))
'''
    return _run_script(script, 15.0)


def build_payload() -> dict[str, Any]:
    """组装 /host-net 的返回体。永远不抛异常。"""
    uplink = _query_uplink()
    # query_host_wifi 依赖宿主侧的 NETPULSE_FORCE_BRIDGE=1（start.sh 已设）。
    wifi = query_host_wifi(use_cache=True)
    payload: dict[str, Any] = {
        "ok": True,
        "source": BRIDGE_SOURCE,
        "uplink": None,
        "media": None,
        "adapter": None,
        "link_speed": None,
        "uplink_status": None,
        "uplink_error": None,
        "wifi": wifi,
    }
    if uplink.get("ok"):
        payload["uplink"] = uplink.get("name")
        payload["media"] = uplink.get("media")
        payload["adapter"] = uplink.get("desc")
        payload["link_speed"] = uplink.get("link_speed")
        payload["uplink_status"] = uplink.get("status")
    else:
        payload["uplink_error"] = uplink.get("error", "未能确定上网网卡")
    return payload


# ---------------------------------------------------------------------------
# 后台刷新 + 缓存（请求只取缓存，绝不现查 powershell）
# ---------------------------------------------------------------------------

def _build_and_store() -> None:
    """取一次宿主网卡数据并写入缓存；永不抛出。"""
    global _PAYLOAD
    try:
        payload = build_payload()
    except Exception as exc:  # noqa: BLE001
        payload = {"ok": False, "error": f"桥接查询异常：{exc}", "wifi": {}}
    with _LOCK:
        _PAYLOAD = payload


def _refresher() -> None:
    """后台线程：周期性刷新缓存。"""
    while True:
        _build_and_store()
        time.sleep(_REFRESH_INTERVAL)


def get_payload() -> dict[str, Any]:
    """瞬时返回最新缓存（HTTP 请求路径，绝不被 powershell 拖慢）。"""
    with _LOCK:
        return dict(_PAYLOAD)


# ---------------------------------------------------------------------------
# 最小 HTTP 服务
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/host-net", "/host_net"):
            # 只返回缓存，瞬时响应（见模块 docstring 的「为什么用缓存」）。
            try:
                self._send(200, get_payload())
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"ok": False, "error": f"桥接查询异常：{exc}"})
        elif path in ("/", "/healthz", "/health"):
            self._send(200, {"ok": True, "service": "netpulse-host-bridge"})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, *args: Any) -> None:  # 静默访问日志
        return


def main(argv: list[str] | None = None) -> int:
    port = DEFAULT_PORT
    if argv:
        for a in argv:
            if a.isdigit():
                port = int(a)

    # 首次同步取数：保证端口起来时缓存已带数据（最多被一次 powershell 超时拖慢，
    # 不会让首请求返回「尚未初始化」）。
    sys.stderr.write("[host-bridge] 首次采集宿主网卡（可能需要几秒）...\n")
    sys.stderr.flush()
    _build_and_store()

    # 后台周期刷新
    threading.Thread(target=_refresher, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"[host-bridge] 监听 0.0.0.0:{port}  (GET /host-net，数据每 {_REFRESH_INTERVAL}s 后台刷新)")
    print(
        "[host-bridge] 容器通过 http://host.docker.internal:"
        f"{port}/host-net 取宿主网卡数据"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
