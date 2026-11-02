"""
Windows 宿主机网络桥接（WSL 专用）—— 让 NetPulse 看见真正的上网网卡。

===========================================================================
为什么需要这个模块
===========================================================================

WSL2 是一个独立的 Linux 虚拟机，有自己的网络命名空间。虚拟机里只有：

    eth0  —— WSL 虚拟网卡（流量最终经 Windows 宿主机的虚拟交换机出去）
    lo     —— 回环

**Windows 上的 WiFi 网卡（WLAN）在 WSL 里根本不存在**——`ip addr` 看不到，
Netlink 读不到，eBPF 探针也采不到它的流量。于是本项目原样跑起来只能监控
eth0：而用户上网走的是 Windows 的 WiFi，监控 eth0 等于监控了一段没人用的
虚拟链路——项目失去意义。

===========================================================================
桥接原理
===========================================================================

WSL 的进程可以通过 interop 直接启动 Windows 可执行文件。所以从 WSL 里调
``powershell.exe``，让 Windows 侧自己去查它自己的网卡：

    netsh wlan show interfaces      SSID / 信号 / 信道 / 链路速率 / 认证方式
    Get-NetAdapter                  网卡状态 / MediaType / 协商速率
    Get-NetAdapterStatistics        网卡累计收发字节（两次采样求差 = 吞吐速率）
    Get-NetConnectionProfile        IPv4 连通性
    ping -n N <target>              真实 WiFi 链路的 RTT / 抖动 / 丢包率

一条 PowerShell 全拿到，解析成结构化数据返回。**在 WSL 之外（原生 Linux）
本模块自动禁用**，原生 Linux 有真正的 wlan0 + wpa_supplicant，走原路径。

===========================================================================
编码：这个项目里最隐蔽的一个坑（实测结论，不是推测）
===========================================================================

中文 Windows 上这条链路有三层编码问题，每一层都能让数据静默出错：

1. **netsh 重定向输出是 UTF-8**（本机实测：字节流 ``e7 b3 bb`` = "系"）。
   但 PowerShell 用**管道**接原生命令输出时，按系统 ANSI 代码页（936）解码，
   UTF-8 字节被当 GBK 解 → "系统"变"绯荤粫"这类乱码，且**不报错**。
2. 旧代码用 ``popen("powershell -Command '...'")`` 正是踩的这条管道。
   表现：SSID 能解析（ASCII 幸存），"信号"/"已连接"全丢 → RSSI 永远是
   -1000，用户看到"信号极差"的假数据。
3. ``[Console]::OutputEncoding`` 在 stdout 是管道时**不生效**（没有控制台），
   设了也没用——实测验证过。

本模块的解法（三层全部确定性处理，不依赖任何环境代码页）：

* 脚本经 ``-EncodedCommand``（Base64(UTF-16LE)）传入，PowerShell 按 UTF-16
  解码，**脚本里的中文正则字面量不会乱**（无 BOM 的 .ps1 会被 5.1 当 GBK 读，
  这是另一个大坑）。
* netsh/ping 用 ``ProcessStartInfo.StandardOutputEncoding`` **显式指定解码}:
  按 utf-8 → gb18030 → big5 → shift_jis → windows-1252 顺序尝试，用
  "信号/Signal"标签校验自动选对（新系统 netsh 出 UTF-8，老系统出 GBK，
  两种情况都能命中）。
* 最终结果 Base64 后只打印 ASCII，**输出管道用任何代码页都不会错**。

===========================================================================
和 C++ 服务端的关系
===========================================================================

``server/src/net_wifiriss.cpp`` 里有一份同原理的 C++ 实现（HealthCheck 的
``wifi`` 字段由它注入）。本模块是**Python 侧的冗余通道**：

* C++ 服务没重新编译 / 编译失败时，Agent 照样能拿到 WiFi 数据；
* Python 侧解析更稳（JSON 原生），且可以单独跑 CLI 验证：
      python host_bridge.py            # 人类可读报告
      python host_bridge.py --json     # 原始 JSON

两边字段名保持一致，netpulse_client.health_check() 会优先采用更新的数据。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 数据来源标记。与 C++ 端 dbus_service.cpp 的 wifiInfoJson 保持一致。
BRIDGE_SOURCE = "windows_host_bridge"

DEFAULT_PING_TARGET = "223.5.5.5"  # 阿里云公共 DNS，国内稳定
DEFAULT_PING_COUNT = 5
DEFAULT_TIMEOUT = 25.0  # netsh + cmdlet + ping -n 5 的最坏耗时约 8s，留足余量

#: 查询结果的内存缓存 TTL（秒）。get_conn_stats 和 get_network_health 会
#: 连续调两次 health_check，不缓存就会连起两个 PowerShell（各带一次 ping）。
_CACHE_TTL = 2.5

#: 以太网卡在 Get-NetAdapter 下的 MediaType；WiFi 固定是 Native 802.11。
_MEDIA_TYPE_WIFI = "Native 802.11"

# ---------------------------------------------------------------------------
# PowerShell 桥接脚本
# ---------------------------------------------------------------------------
# 这段脚本同时以同原理嵌在 server/src/net_wifiriss.cpp 里（那边输出 key=value，
# 这边输出 JSON）。**改动时必须同步两边**，字段名是两边的契约。
#
# 注意：脚本里有中文正则字面量，依赖 -EncodedCommand 的 UTF-16 传输，
# 不要改成 "-File 无BOM的.ps1" 或 "-Command '...'"（引号 + 编码双坑）。

_PS_SCRIPT_TEMPLATE = r'''
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# 以【显式编码】读取一个 Windows 原生命令的输出。
#
# 为什么不能用管道 `netsh ... | Out-String`：PowerShell 会按系统 ANSI 代码页
# 解码原生命令的输出，中文 Windows 上 netsh 实际吐的是 UTF-8，一解就乱，而且
# 不报错。StandardOutputEncoding 是 .NET 侧对流的直接解码，可完全指定。
#
# 编码候选顺序 + 标记校验：新系统 netsh 输出 UTF-8；老系统（Win7/早期Win10）
# 输出 GBK。用 "信号/Signal" 这类【标签】做标记——不能拿 SSID 当标记，
# 它是 ASCII，任何编码都能"解码成功"，没有判别力。
# ---------------------------------------------------------------------------
function Get-NativeText {
  param([string[]]$ExeCandidates, [string]$Arguments, [string]$MarkerRe)
  $first = $null
  foreach ($exe in $ExeCandidates) {
    foreach ($encName in @('utf-8', 'gb18030', 'big5', 'shift_jis', 'windows-1252')) {
      $text = $null
      try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $exe
        $psi.Arguments = $Arguments
        $psi.RedirectStandardOutput = $true
        $psi.UseShellExecute = $false
        $psi.StandardOutputEncoding = [System.Text.Encoding]::GetEncoding($encName)
        $proc = [System.Diagnostics.Process]::Start($psi)
        $text = $proc.StandardOutput.ReadToEnd()
        $proc.WaitForExit()
      } catch { continue }
      if ($null -eq $first) { $first = $text }
      if ($MarkerRe) {
        if ($text -match $MarkerRe) { return $text }
      } else {
        return $text
      }
    }
    # 这个 exe 至少成功跑起来过一次：换 exe 没意义（问题不在 exe），
    # 换编码已经在上面试完了。返回能拿到的最好结果。
    if ($first) { break }
  }
  return $first
}

# ---------------------------------------------------------------------------
# 从 netsh 文本里按【标签】取值。中英文双语：中文系统出中文标签，
# 英文系统出英文标签，两套都要认。标签用行首锚定，避免误匹配
# （比如 "AP BSSID" 行不能当成 "BSSID" 命中）。
# ---------------------------------------------------------------------------
function Get-Field {
  param([string]$Text, [string[]]$Labels)
  if ([string]::IsNullOrEmpty($Text)) { return '' }
  foreach ($label in $Labels) {
    $m = [regex]::Match($Text, '(?m)^\s*' + [regex]::Escape($label) + '\s*:\s*(.*)$')
    if ($m.Success) { return $m.Groups[1].Value.Trim() }
  }
  return ''
}

function To-IntOrNull([string]$v) {
  if ($v -match '^\d+$') { return [int]$v }
  return $null
}

$markerRe = '(?m)^\s*(?:信号|Signal|電波強度|接收速率|Receive rate)\s*:'
$netshExe = @('netsh', ($env:SystemRoot + '\System32\netsh.exe'), 'C:\Windows\System32\netsh.exe')
$netshText = Get-NativeText $netshExe 'wlan show interfaces' $markerRe

$ifName  = Get-Field $netshText @('接口名称', '名称', 'Name')
$ifDesc  = Get-Field $netshText @('说明', '描述', 'Description')
$state   = Get-Field $netshText @('状态', 'State')
$ssid    = Get-Field $netshText @('SSID')
$bssid   = Get-Field $netshText @('AP BSSID', 'BSSID')
$band    = Get-Field $netshText @('波段', '频段', '頻帶', 'Band')
$channel = Get-Field $netshText @('通道', '信道', '頻道', 'Channel')
$radio   = Get-Field $netshText @('无线电类型', '無線類型', 'Radio type')
$auth    = Get-Field $netshText @('身份验证', '驗證', 'Authentication')
$cipher  = Get-Field $netshText @('密码', '加密', 'Cipher')
$rxMbps  = Get-Field $netshText @('接收速率(Mbps)', '接收速率 (Mbps)', 'Receive rate (Mbps)')
$txMbps  = Get-Field $netshText @('传输速率 (Mbps)', '传输速率(Mbps)', 'Transmit rate (Mbps)')
$signal  = Get-Field $netshText @('信号', 'Signal')

# --- 网卡对象信息（cmdlet 返回对象，无编码问题；MediaType 是 locale 无关的 WiFi 判定）---
$ad = $null
if ($ifName) { $ad = Get-NetAdapter -Name $ifName -ErrorAction SilentlyContinue }
if (-not $ad) {
  $ad = Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.MediaType -eq '__MEDIA_TYPE_WIFI__' } |
        Select-Object -First 1
}
$adName    = if ($ad) { [string]$ad.Name } else { '' }
$adDesc    = if ($ad) { [string]$ad.InterfaceDescription } else { '' }
$adStatus  = if ($ad) { [string]$ad.Status } else { '' }
$mediaType = if ($ad) { [string]$ad.MediaType } else { '' }
# LinkSpeed 是字符串（"459 Mbps"），强转数值会静默失败；数值型的是
# ReceiveLinkSpeed / TransmitLinkSpeed（单位 bps）。
$linkRxBps = 0
$linkTxBps = 0
if ($ad) {
  if ($ad.ReceiveLinkSpeed) { $linkRxBps = [int64]$ad.ReceiveLinkSpeed }
  if ($ad.TransmitLinkSpeed) { $linkTxBps = [int64]$ad.TransmitLinkSpeed }
}
$linkSpeedText = if ($ad) { [string]$ad.LinkSpeed } else { '' }

# --- 累计字节数（两次采样求差即吞吐速率，采样状态在调用方维护）---
$stats = $null
if ($adName) { $stats = Get-NetAdapterStatistics -Name $adName -ErrorAction SilentlyContinue }
if (-not $stats) {
  $stats = Get-NetAdapterStatistics -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -match 'Wi-?Fi|WLAN|Wireless|802\.11|无线' } |
           Select-Object -First 1
}
$rxBytes = 0
$txBytes = 0
if ($stats) { $rxBytes = [int64]$stats.ReceivedBytes; $txBytes = [int64]$stats.SentBytes }

# --- 连接 profile（IPv4 连通性）---
$prof = $null
if ($adName) { $prof = Get-NetConnectionProfile -InterfaceAlias $adName -ErrorAction SilentlyContinue }
if (-not $prof) { $prof = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Select-Object -First 1 }
$ipv4Conn  = if ($prof) { [string]$prof.IPv4Connectivity } else { '' }
$netCateg  = if ($prof) { [string]$prof.NetworkCategory } else { '' }

# --- ping：真实链路 RTT / 抖动 / 丢包。TTL 行是 locale 无关的（中英文输出都含 "TTL="），
#     时间数字后面的 ms 也是 ASCII，所以任何 ASCII 兼容编码都能解析。---
$pingExe = @('ping', ($env:SystemRoot + '\System32\PING.EXE'), 'C:\Windows\System32\PING.EXE')
$pingText = Get-NativeText $pingExe ('-n __PING_COUNT__ __PING_TARGET__') ''
$rtts = New-Object System.Collections.Generic.List[int]
foreach ($line in ($pingText -split "`r?`n")) {
  if ($line -match 'TTL') {
    $m = [regex]::Match($line, '(\d+)\s*ms')
    if ($m.Success) { [void]$rtts.Add([int]$m.Groups[1].Value) }
  }
}
$replies = $rtts.Count
$lossPct = 0.0
if (__PING_COUNT__ -gt 0 -and $replies -lt __PING_COUNT__) {
  $lossPct = [math]::Round((__PING_COUNT__ - $replies) * 100.0 / __PING_COUNT__, 2)
}

# --- 信号百分比 → dBm 估算。Windows 只给百分比，拿不到真实 dBm
#     （WMI MSNdis_80211_ReceivedSignalStrength 在新驱动上普遍不可用，实测报"不支持"）。
#     换算公式与常见监控实践一致：0% ≈ -100dBm，100% ≈ -50dBm。
#     必须同时上报原始百分比和 estimated 标记，上层不能把估算当真值。---
$signalPct = 0
if ($signal -match '(\d+)') { $signalPct = [int]$Matches[1] }
$rssi = $null
if ($signalPct -gt 0) { $rssi = [int][math]::Round($signalPct / 2.0) - 100 }

# --- 连接状态归一化。中文输出"已连接"、英文"connected"；解码失败时用
#     adapter Status 和 SSID 是否存在兜底——宁可在 note 里存疑，不要静默丢字段。---
$stateNorm = 'unknown'
if ($state -match '(?i)connected|已连接|已連線|连接中|連接中') { $stateNorm = 'connected' }
elseif ($state -match '(?i)disconnected|已断开|已斷開|断开|斷開') { $stateNorm = 'disconnected' }
elseif ($adStatus -eq 'Up' -and $ssid) { $stateNorm = 'connected' }
elseif ($adStatus -eq 'Disconnected') { $stateNorm = 'disconnected' }

$result = [PSCustomObject][ordered]@{
  ok                 = $true
  source             = '__BRIDGE_SOURCE__'
  adapter_name       = $(if ($ifName) { $ifName } else { $adName })
  adapter_desc       = $(if ($ifDesc) { $ifDesc } else { $adDesc })
  adapter_status     = $adStatus
  media_type         = $mediaType
  state              = $stateNorm
  ssid               = $ssid
  bssid              = $bssid
  band               = $band
  channel            = $(if ($null -ne (To-IntOrNull $channel)) { To-IntOrNull $channel } else { $channel })
  radio_type         = $radio
  authentication     = $auth
  cipher             = $cipher
  signal_pct         = $signalPct
  rssi_dbm           = $rssi
  rssi_estimated     = [bool]($null -ne $rssi)
  receive_mbps       = To-IntOrNull $rxMbps
  transmit_mbps      = To-IntOrNull $txMbps
  link_speed_text    = $linkSpeedText
  link_rx_bps        = $linkRxBps
  link_tx_bps        = $linkTxBps
  link_speed_bps     = $(if ($linkRxBps -gt $linkTxBps) { $linkRxBps } else { $linkTxBps })
  rx_bytes           = $rxBytes
  tx_bytes           = $txBytes
  ipv4_connectivity  = $ipv4Conn
  network_category   = $netCateg
  ping_target        = '__PING_TARGET__'
  ping_count         = __PING_COUNT__
  ping_replies       = $replies
  rtt_samples        = @($rtts)
  tcp_loss_pct       = $lossPct
  netsh_available    = [bool]$netshText
  adapter_found      = [bool]$ad
}

$json = $result | ConvertTo-Json -Depth 4 -Compress
$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($json))
[Console]::WriteLine('NETPULSE_OK ' + $b64)
'''


def _build_script(ping_target: str, ping_count: int) -> str:
    """把参数烘进脚本模板。

    参数只允许 ASCII 安全字符——它们会被拼进 PowerShell 命令行上下文，
    虽然走 -EncodedCommand 没有 shell 引号问题，仍要防注入。
    """
    target = re.sub(r"[^A-Za-z0-9.\-:]", "", ping_target or "") or DEFAULT_PING_TARGET
    count = max(1, min(int(ping_count or DEFAULT_PING_COUNT), 20))
    return (
        _PS_SCRIPT_TEMPLATE.replace("__PING_TARGET__", target)
        .replace("__PING_COUNT__", str(count))
        .replace("__BRIDGE_SOURCE__", BRIDGE_SOURCE)
        .replace("__MEDIA_TYPE_WIFI__", _MEDIA_TYPE_WIFI)
    )


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------

_WSL_MARKERS = ("microsoft", "wsl")


def is_wsl() -> bool:
    """是否运行在 WSL 里。

    三个证据任一命中即可（/proc/version 在 WSL2 含 "microsoft"；
    WSL 会设置 WSL_DISTRO_NAME / WSL_INTEROP；interop 挂载点存在）。
    原生 Linux 一律返回 False——那边有真 wlan0，不需要桥接。

    ``NETPULSE_FORCE_BRIDGE=1`` 可强制启用：在 Windows 本机（非 WSL）上
    单独调试这条桥接链路时有用。
    """
    if os.environ.get("NETPULSE_FORCE_BRIDGE") == "1":
        return True
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", "r", encoding="ascii", errors="ignore") as fh:
            version = fh.read().lower()
        if any(marker in version for marker in _WSL_MARKERS):
            return True
    except OSError:
        pass
    return os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop") or os.path.exists(
        "/mnt/c/Windows/System32"
    )


def find_powershell() -> str | None:
    """定位 powershell.exe。

    WSL interop 下 Windows 可执行文件直接在 PATH 里可见；sudo 场景 PATH 可能
    被剃光，所以先试 PATH，再试几个已知绝对路径。
    """
    found = shutil.which("powershell.exe")
    if found:
        return found
    for candidate in (
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# 查询执行
# ---------------------------------------------------------------------------

_OK_PREFIX = "NETPULSE_OK "
_FAIL_PREFIX = "NETPULSE_FAIL "


def _run_script(script: str, timeout: float) -> dict[str, Any]:
    """以 -EncodedCommand 方式执行脚本，解析 Base64 结果。

    -EncodedCommand 收 Base64(UTF-16LE)：中文正则字面量随 UTF-16 安全到达，
    不受 .ps1 文件编码（无 BOM 会被当 GBK）和 shell 引号影响。
    """
    ps = find_powershell()
    if ps is None:
        return {"ok": False, "error": "找不到 powershell.exe（不在 WSL 里？）"}
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(  # noqa: S603 - 固定可执行文件 + 固定参数
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"PowerShell 查询超时（>{timeout:.0f}s）"}
    except OSError as exc:
        return {"ok": False, "error": f"启动 powershell.exe 失败：{exc}"}

    stdout = proc.stdout.decode("ascii", errors="replace")
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(_OK_PREFIX):
            try:
                payload = base64.b64decode(line[len(_OK_PREFIX):]).decode("utf-8")
                return json.loads(payload)
            except Exception as exc:  # noqa: BLE001 - 任何解析失败都降级为 error dict
                return {"ok": False, "error": f"解析桥接返回失败：{exc}"}
        if line.startswith(_FAIL_PREFIX):
            try:
                reason = base64.b64decode(line[len(_FAIL_PREFIX):]).decode("utf-8")
            except Exception:  # noqa: BLE001
                reason = "未知原因"
            return {"ok": False, "error": f"Windows 侧查询失败：{reason}"}
    return {
        "ok": False,
        "error": (
            "PowerShell 未返回预期输出（可能不是 WSL，或 interop 被禁用）。"
            f" rc={proc.returncode}"
        ),
    }


# ---------------------------------------------------------------------------
# 速率计算（两次采样求差）与缓存
# ---------------------------------------------------------------------------

_RATE_STATE: dict[str, Any] = {"rx": 0, "tx": 0, "t": 0.0, "jitter": 0.0, "last_rtt": -1}
_QUERY_CACHE: dict[str, Any] = {"data": None, "at": 0.0}


def _apply_rates(data: dict[str, Any]) -> dict[str, Any]:
    """用上一次采样的累计字节数算吞吐速率；用连续 RTT 样本算抖动。

    与 C++ 端 net_wifiriss.cpp 的同名逻辑保持一致：
    * dt <= 0.1s 不计算（防止缓存命中导致除零/虚高）；
    * 网卡计数器的累计值可能被重置（休眠唤醒），只增不算差值；
    * 样本数 < 2 时抖动沿用上一次的 EWMA，RTT 无效时抖动归零。
    """
    now = time.monotonic()
    rx = int(data.get("rx_bytes") or 0)
    tx = int(data.get("tx_bytes") or 0)
    dt = now - _RATE_STATE["t"]
    if _RATE_STATE["t"] > 0 and dt > 0.1:
        if rx >= _RATE_STATE["rx"]:
            data["download_bps"] = round((rx - _RATE_STATE["rx"]) / dt, 1)
        if tx >= _RATE_STATE["tx"]:
            data["upload_bps"] = round((tx - _RATE_STATE["tx"]) / dt, 1)
        # 速率是怎么算出来的 —— 消费方（agent 的 measure_network_performance）
        # 靠它区分"真的没有流量"和"这次采样之前没有基准、算不出速率"。
        # 没有这个字段，两者都表现成 0，上层只能猜。
        data["rate_basis"] = "byte_delta"
    else:
        data["rate_basis"] = "no_baseline"
    data.setdefault("download_bps", 0.0)
    data.setdefault("upload_bps", 0.0)

    samples = [int(v) for v in (data.get("rtt_samples") or []) if str(v).lstrip("-").isdigit()]
    if samples:
        data["rtt_ms"] = int(round(sum(samples) / len(samples)))
        data["rtt_samples"] = samples
        if len(samples) >= 2:
            jitter = sum(abs(b - a) for a, b in zip(samples, samples[1:])) / (len(samples) - 1)
            data["jitter_ms"] = round(jitter, 2)
            _RATE_STATE["jitter"] = data["jitter_ms"]
        else:
            data["jitter_ms"] = _RATE_STATE["jitter"]
    else:
        data["rtt_ms"] = None
        data["jitter_ms"] = 0.0
    if data.get("rtt_ms") is None:
        data["jitter_ms"] = 0.0

    _RATE_STATE["rx"] = rx
    _RATE_STATE["tx"] = tx
    _RATE_STATE["t"] = now
    if data.get("rtt_ms") is not None:
        _RATE_STATE["last_rtt"] = data["rtt_ms"]

    # 与 C++ 端 wifiInfoJson 对齐的字段名/别名，老消费者不认新名字
    data["interface"] = "wlan0"
    data["is_host_bridge"] = True
    data["has_traffic"] = bool(data.get("download_bps") or data.get("upload_bps"))
    data["link_rx_mbps"] = data.get("receive_mbps")
    data["link_tx_mbps"] = data.get("transmit_mbps")
    data["using_now"] = data.get("state") == "connected"
    data["fetched_at"] = round(time.time(), 3)
    data["note"] = (
        "WiFi metrics are measured on the Windows host adapter via "
        "netsh/Get-NetAdapterStatistics/ping; eBPF inside WSL monitors eth0."
    )
    return data


def query_host_wifi(
    ping_target: str | None = None,
    ping_count: int = DEFAULT_PING_COUNT,
    use_cache: bool = True,
) -> dict[str, Any]:
    """查询 Windows 宿主机 WiFi 的完整指标。

    返回 dict（**永远不抛异常**，与 ToolRunner 的铁律一致）：

        成功: {"ok": True, "ssid": ..., "rssi_dbm": ..., "rtt_ms": ...,
               "download_bps": ..., "tcp_loss_pct": ..., ...}
        失败: {"ok": False, "error": "..."}

    ``rssi_dbm`` 是由信号百分比估算的（``rssi_estimated: True``），
    Windows 不暴露真实 dBm——消费方应当同时展示 ``signal_pct``。
    """
    if not is_wsl():
        return {"ok": False, "error": "不在 WSL 环境中，无需 Windows 宿主机桥接"}
    if use_cache and _QUERY_CACHE["data"] is not None:
        if time.monotonic() - _QUERY_CACHE["at"] < _CACHE_TTL:
            cached = dict(_QUERY_CACHE["data"])
            cached["from_cache"] = True
            return cached

    target = ping_target or os.environ.get("WEAKNET_PING_TARGET") or DEFAULT_PING_TARGET
    data = _run_script(_build_script(target, ping_count), DEFAULT_TIMEOUT)
    if not data.get("ok"):
        return data
    # 没有 SSID 也没有 WiFi 网卡：机器大概率在用有线上网，或者 netsh 整个失败。
    # 这两种情况对上层是同一个语义——"没有 WiFi 可报"，必须和"查询出错"区分开。
    if not data.get("ssid") and not data.get("adapter_found"):
        return {
            "ok": False,
            "error": "未发现 WiFi 无线网卡（可能正在使用有线上网，或 netsh 查询失败）",
            "raw": data,
        }
    data = _apply_rates(data)
    _QUERY_CACHE["data"] = dict(data)
    _QUERY_CACHE["at"] = time.monotonic()
    return data


def clear_cache() -> None:
    """清空查询缓存与速率采样状态（测试用）。"""
    _QUERY_CACHE["data"] = None
    _QUERY_CACHE["at"] = 0.0
    _RATE_STATE.update({"rx": 0, "tx": 0, "t": 0.0, "jitter": 0.0, "last_rtt": -1})


def peek_cached_host_wifi(max_age: float = 30.0) -> dict[str, Any] | None:
    """只读缓存，**不发起新查询**。

    给 list_interfaces 这类"有就用、没有拉倒"的场景：WSL 里为了一串网卡名
    去起一个 PowerShell（还带 5 次 ping）不值当。健康检查跑过之后缓存就是
    热的，这里直接取；没跑过就返回 None，调用方自己降级。
    """
    if _QUERY_CACHE["data"] is None:
        return None
    if time.monotonic() - _QUERY_CACHE["at"] >= max_age:
        return None
    cached = dict(_QUERY_CACHE["data"])
    cached["from_cache"] = True
    return cached


# ---------------------------------------------------------------------------
# 人类可读报告 + CLI
# ---------------------------------------------------------------------------


def format_report(wifi: dict[str, Any]) -> str:
    """把查询结果格式化为终端报告（CLI 与排障用）。"""
    if not wifi.get("ok"):
        return f"Windows 宿主机 WiFi 查询失败：{wifi.get('error', '未知原因')}"

    def line(label: str, value: Any) -> str:
        return f"  {label:<16}{value}"

    rssi = wifi.get("rssi_dbm")
    rssi_txt = "无" if rssi is None else f"{rssi} dBm"
    if wifi.get("rssi_estimated"):
        rssi_txt += f"（由信号 {wifi.get('signal_pct')}% 估算）"
    rtt = wifi.get("rtt_ms")
    rtt_txt = "未测出" if rtt is None else f"{rtt} ms"
    loss = wifi.get("tcp_loss_pct")
    loss_txt = "未测出" if loss is None else f"{loss}%"
    down = wifi.get("download_bps") or 0
    up = wifi.get("upload_bps") or 0
    mbps = lambda b: f"{b / 1024 / 1024:.2f} MB/s"  # noqa: E731

    return "\n".join(
        [
            "Windows 宿主机 WiFi（经 WSL 桥接采集）",
            "=" * 44,
            line("网卡", wifi.get("adapter_name") or "?"),
            line("网卡型号", wifi.get("adapter_desc") or "?"),
            line("状态", wifi.get("state") or "?"),
            line("SSID", wifi.get("ssid") or "(未连接)"),
            line("BSSID", wifi.get("bssid") or "-"),
            line("信号", f"{wifi.get('signal_pct', 0)}%  {rssi_txt}"),
            line("信道/频段", f"{wifi.get('channel') or '-'} / {wifi.get('band') or '-'}"),
            line("无线类型", wifi.get("radio_type") or "-"),
            line("认证方式", wifi.get("authentication") or "-"),
            line(
                "链路速率",
                f"收 {wifi.get('receive_mbps') or '-'} / 发 {wifi.get('transmit_mbps') or '-'} Mbps",
            ),
            line("RTT", rtt_txt + f"（{wifi.get('ping_replies', 0)}/{wifi.get('ping_count', 0)} 回复）"),
            line("抖动", f"{wifi.get('jitter_ms', 0)} ms"),
            line("丢包率", loss_txt + f"（ping {wifi.get('ping_target')}）"),
            line("下载/上传", f"{mbps(down)} / {mbps(up)}"),
            line("IPv4 连通性", wifi.get("ipv4_connectivity") or "-"),
            line("采集时间", time.strftime("%H:%M:%S", time.localtime(wifi.get("fetched_at", 0)))),
            "",
            "注：RSSI 为百分比估算值（Windows 不暴露真实 dBm）；",
            "    速率由两次采样累计字节求差得出，首次查询为 0。",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="查询 Windows 宿主机 WiFi 信息（WSL 桥接）",
    )
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    parser.add_argument("--ping-target", default=None, help="ping 目标（默认 223.5.5.5）")
    parser.add_argument("--ping-count", type=int, default=DEFAULT_PING_COUNT, help="ping 次数")
    parser.add_argument("--no-cache", action="store_true", help="绕过缓存")
    args = parser.parse_args(argv)

    wifi = query_host_wifi(
        ping_target=args.ping_target,
        ping_count=args.ping_count,
        use_cache=not args.no_cache,
    )
    if args.json:
        print(json.dumps(wifi, ensure_ascii=False, indent=2))
    else:
        print(format_report(wifi))
    return 0 if wifi.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
