// test_wifi_bridge.cpp
// net_wifiriss_bridge.cpp（WSL 宿主机 WiFi 桥接）的测试 harness。
//
// 分三部分：
//   1. 离线单测——纯函数：
//      · base64 / UTF-16LE 转换（RFC 4648 向量 + 中文/emoji/代理对/坏字节）
//      · netsh 输出解析：**真实中文 Windows 捕获样本**做黄金固件（含全角冒号
//        的 QoS 行、不规整填充空格、"接收速率(Mbps)" 无空格 vs
//        "传输速率 (Mbps)" 有空格），外加英文样本与断开态样本
//      · ping 输出解析：中文（GBK 原始字节）/英文/亚毫秒/超时四种形态
//      · 状态归一化
//      · PowerShell 短脚本的命令行长度 < 8191（popen 走 sh -c 的硬上限，
//        曾因整包脚本 ~17KB 直接爆掉——这是那条坑的回归测试）
//   2. 在线集成——对着真实 Windows（WSL 里天然满足；Windows 开发机可强制
//      WSL_DISTRO_NAME=1）跑完整三路桥接，打印全部字段人工核对，
//      并与 agent/host_bridge.py 的输出做字段级比对（契约）。
//   3. 缓存行为——peek 的 TTL 语义。
//
// 编译（MinGW）：
//   g++ -std=c++17 -Wall -Wextra -Iinclude -o build/test_wifi_bridge tests/test_wifi_bridge.cpp
// 运行：
//   ./build/test_wifi_bridge                    # 只跑离线单测 + 缓存语义
//   WSL_DISTRO_NAME=1 ./build/test_wifi_bridge  # 追加在线集成（非 WSL 需强制变量）

#include "../src/net_wifiriss_bridge.cpp"

#include <cstdio>
#include <string>
#include <thread>
#include <vector>

namespace {

int g_failures = 0;

void CHECK(bool cond, const char* what) {
    if (cond) {
        std::printf("  [PASS] %s\n", what);
    } else {
        std::printf("  [FAIL] %s\n", what);
        ++g_failures;
    }
}

std::string b64(const std::string& s) {
    return base64Encode(reinterpret_cast<const uint8_t*>(s.data()), s.size());
}

// ---- RFC 4648 测试向量：1/2/3 字节余数各覆盖到 ----
void testBase64Vectors() {
    std::printf("[offline] base64 RFC 4648 vectors\n");
    CHECK(b64("") == "", "'' -> ''");
    CHECK(b64("f") == "Zg==", "'f' -> 'Zg=='");
    CHECK(b64("fo") == "Zm8=", "'fo' -> 'Zm8='");
    CHECK(b64("foo") == "Zm9v", "'foo' -> 'Zm9v'");
    CHECK(b64("foob") == "Zm9vYg==", "'foob' -> 'Zm9vYg=='");
    CHECK(b64("fooba") == "Zm9vYmE=", "'fooba' -> 'Zm9vYmE='");
    CHECK(b64("foobar") == "Zm9vYmFy", "'foobar' -> 'Zm9vYmFy'");
}

void testBase64RoundTrip() {
    std::printf("[offline] base64 round-trip\n");
    const std::vector<std::string> samples = {
        "", "f", "fo", "foo", "foob", "fooba", "foobar",
        std::string(255, 'A'), std::string(256, 'B'), std::string(257, 'C'),
        "NETPULSE_OK payload with spaces and = signs",
    };
    bool allOk = true;
    for (const auto& s : samples) {
        std::string dec;
        if (!base64Decode(b64(s), dec) || dec != s) {
            allOk = false;
            std::printf("    round-trip failed for len=%zu\n", s.size());
        }
    }
    CHECK(allOk, "round-trip over 11 samples (len 0..257)");

    std::string dec;
    CHECK(!base64Decode("!!!!", dec), "reject illegal chars");
}

// ---- UTF-8 → UTF-16LE：ASCII / 中文 / 代理对 / 坏字节 ----
void testUtf8ToUtf16Le() {
    std::printf("[offline] utf8 -> utf16le\n");
    CHECK(utf8ToUtf16Le("A") == (std::vector<uint8_t>{0x41, 0x00}), "'A' -> 41 00");
    CHECK(utf8ToUtf16Le("\xE4\xBF\xA1") == (std::vector<uint8_t>{0xE1, 0x4F}),
          "U+4FE1 -> E1 4F");
    CHECK(utf8ToUtf16Le("\xE4\xBF\xA1\xE5\x8F\xB7") ==
              (std::vector<uint8_t>{0xE1, 0x4F, 0xF7, 0x53}),
          "U+4FE1 U+53F7 -> E1 4F F7 53");
    CHECK(utf8ToUtf16Le("\xF0\x9F\x8E\xB5") ==
              (std::vector<uint8_t>{0x3C, 0xD8, 0xB5, 0xDF}),
          "U+1F3B5 -> surrogate pair 3C D8 B5 DF");
    // 孤立后续字节 -> U+FFFD（UTF-16LE: FD FF，低位在前），且只跳过一个字节
    CHECK(utf8ToUtf16Le("\x80") == (std::vector<uint8_t>{0xFD, 0xFF}),
          "lone continuation -> U+FFFD");
    CHECK(utf8ToUtf16Le("\x80" "A") == (std::vector<uint8_t>{0xFD, 0xFF, 0x41, 0x00}),
          "lone continuation then 'A'");
    // 被截断的多字节序列 -> U+FFFD + 剩余字节按孤立后续字节处理
    CHECK(utf8ToUtf16Le("\xE4\xBF") == (std::vector<uint8_t>{0xFD, 0xFF, 0xFD, 0xFF}),
          "truncated 3-byte seq -> U+FFFD U+FFFD");
}

// ---- netsh 解析：真实中文 Windows 捕获样本（黄金固件）----
void testNetshChineseGolden() {
    std::printf("[offline] netsh parse: real zh-CN golden sample\n");
    // 2024 年从一台中文 Windows 11 的 `netsh wlan show interfaces` 原样捕获
    // （UTF-8 字节，\r\n 行尾，填充空格不规整，含全角冒号的 QoS 行）。
    const std::string sample =
        "\r\n"
        "系统上有 1 个接口: \r\n"
        "\r\n"
        "    名称                   : WLAN\r\n"
        "    说明            : Intel(R) Wi-Fi 6E AX211 160MHz\r\n"
        "    GUID                   : 97d7ff2a-864e-4303-90d9-c709670959c9\r\n"
        "    物理地址       : 4c:5f:70:76:63:0d\r\n"
        "    界面类型         : 主要\r\n"
        "    状态                  : 已连接\r\n"
        "    SSID                   : ZZGS\r\n"
        "    AP BSSID               : 5a:81:d4:0b:70:e6\r\n"
        "    波段                   : 5 GHz\r\n"
        "    通道                : 157\r\n"
        "    网络类型               : 结构\r\n"
        "    无线电类型             : 802.11ax\r\n"
        "    身份验证               : 开放式\r\n"
        "    密码                 : 无\r\n"
        "    连接模式        : 配置文件\r\n"
        "    接收速率(Mbps)         : 310\r\n"
        "    传输速率 (Mbps)        : 293\r\n"
        "    信号                   : 71%\r\n"
        "    配置文件               : ZZGS \r\n"
        "已配置     QoS MSCS： 0\r\n"
        "    已配置 QoS 映射： 0\r\n"
        "    策略允许的 QoS 映射 ： 0\r\n"
        "\r\n"
        "    承载网络状态  : 不可用\r\n"
        "\r\n";
    const NetshFields f = parseNetshInterfaces(sample);
    CHECK(f.any, "golden sample matched known fields");
    CHECK(f.name == "WLAN", "name");
    CHECK(f.desc == "Intel(R) Wi-Fi 6E AX211 160MHz", "desc");
    CHECK(f.state == "已连接", "state raw");
    CHECK(f.ssid == "ZZGS", "ssid");
    CHECK(f.bssid == "5a:81:d4:0b:70:e6", "bssid");
    CHECK(f.band == "5 GHz", "band");
    CHECK(f.channel == "157", "channel");
    CHECK(f.radio == "802.11ax", "radio");
    CHECK(f.auth == "开放式", "auth");
    CHECK(f.cipher == "无", "cipher");
    CHECK(f.rxMbps == 310, "rx mbps (no space before paren)");
    CHECK(f.txMbps == 293, "tx mbps (space before paren)");
    CHECK(f.signalPct == 71, "signal pct from '71%'");
    CHECK(normalizeWifiState(f.state, "", f.ssid) == "connected", "state -> connected");
}

void testNetshEnglish() {
    std::printf("[offline] netsh parse: en-US sample\n");
    const std::string sample =
        "There is 1 interface on the system:\r\n"
        "\r\n"
        "    Name                   : WLAN\r\n"
        "    Description            : Intel(R) Wi-Fi 6E AX201\r\n"
        "    State                  : connected\r\n"
        "    SSID                   : MyWiFi\r\n"
        "    AP BSSID               : aa:bb:cc:dd:ee:ff\r\n"
        "    Band                   : 5 GHz\r\n"
        "    Channel                : 36\r\n"
        "    Radio type             : 802.11ac\r\n"
        "    Authentication         : WPA2-Personal\r\n"
        "    Cipher                 : CCMP\r\n"
        "    Receive rate (Mbps)    : 390\r\n"
        "    Transmit rate (Mbps)   : 390\r\n"
        "    Signal                 : 88%\r\n";
    const NetshFields f = parseNetshInterfaces(sample);
    CHECK(f.name == "WLAN", "en name");
    CHECK(f.state == "connected", "en state");
    CHECK(f.ssid == "MyWiFi", "en ssid");
    CHECK(f.band == "5 GHz", "en band");
    CHECK(f.channel == "36", "en channel");
    CHECK(f.radio == "802.11ac", "en radio");
    CHECK(f.auth == "WPA2-Personal", "en auth");
    CHECK(f.cipher == "CCMP", "en cipher");
    CHECK(f.rxMbps == 390, "en rx mbps");
    CHECK(f.signalPct == 88, "en signal pct");
}

void testNetshDisconnectedAndEmpty() {
    std::printf("[offline] netsh parse: disconnected / empty\n");
    const std::string disc =
        "    名称        : WLAN\r\n"
        "    状态        : 已断开\r\n";
    const NetshFields f = parseNetshInterfaces(disc);
    CHECK(f.any, "disconnected sample still matched");
    CHECK(f.name == "WLAN", "disc name");
    CHECK(normalizeWifiState(f.state, "", "") == "disconnected", "disc state");
    // 英文断开
    CHECK(normalizeWifiState("disconnected", "", "") == "disconnected", "en disconnected");
    // netsh 本身失败（空输出/报错）-> any=false，调用方据此判定桥接不可用
    CHECK(!parseNetshInterfaces("").any, "empty netsh output -> no fields");
    CHECK(!parseNetshInterfaces("netsh: command not found\n").any,
          "garbage netsh output -> no fields");
}

// ---- ping 解析：中文(GBK 原始字节) / 英文 / 亚毫秒 / 超时 ----
void testPingParse() {
    std::printf("[offline] ping parse\n");
    // 中文 Windows 的 ping 重定向到管道时输出 GBK(OEM 936) 字节。这一行是
    // 真实捕获："来自 223.5.5.5 的回复: 字节=32 时间=28ms TTL=48"
    const std::string zhGbk =
        "\xC0\xB4\xD7\xD4" " 223.5.5.5 " "\xB5\xC4\xBB\xD8\xB8\xB4" ": "
        "\xD7\xD6\xBD\xDA" "=32 " "\xCA\xB1\xBC\xE4" "=28ms TTL=48\r\n";
    const PingResult zh = parsePingOutput(zhGbk);
    CHECK(zh.rtts.size() == 1 && zh.rtts[0] == 28, "zh-CN GBK reply -> rtt 28");

    const std::string en =
        "Reply from 223.5.5.5: bytes=32 time=19ms TTL=52\r\n"
        "Reply from 223.5.5.5: bytes=32 time<1ms TTL=52\r\n"
        "Ping statistics for 223.5.5.5:\r\n"
        "    Minimum = 18ms, Maximum = 28ms, Average = 21ms\r\n";
    const PingResult e = parsePingOutput(en);
    CHECK(e.rtts.size() == 2, "en: two replies (summary line excluded)");
    CHECK(e.rtts.size() == 2 && e.rtts[0] == 19, "en: first rtt 19");
    CHECK(e.rtts.size() == 2 && e.rtts[1] == 1, "en: time<1ms -> 1");

    // 超时/不可达：没有 TTL 行，不应产出样本
    const std::string timeout =
        "\xC7\xEB\xC7\xF3\xB3\xAC\xCA\xB1\xA1\xA3\r\n";  // "请求超时。" GBK
    CHECK(parsePingOutput(timeout).rtts.empty(), "zh timeout -> no samples");

    const std::string unreachable =
        "Reply from 10.0.0.1: Destination host unreachable.\r\n";
    CHECK(parsePingOutput(unreachable).rtts.empty(), "unreachable -> no samples");
}

// ---- PowerShell 短脚本的命令行长度（8191 上限回归测试）----
void testAdapterStatsCommandLength() {
    std::printf("[offline] adapter-stats command length\n");
    const std::string cmd = buildAdapterStatsCommand("powershell.exe");
    // popen 走 sh -c，实测上限 8191（8066 通过 / 10734 报「命令行太长」）。
    // 整包桥接脚本曾因此爆掉（~17KB）；现在只传 ~600 字符的 ASCII 短脚本。
    CHECK(cmd.size() < 8191, "command line < 8191");
    CHECK(cmd.find("-EncodedCommand ") != std::string::npos, "uses -EncodedCommand");
    CHECK(cmd.find('\n') == std::string::npos, "single line (sh -c safe)");
    CHECK(cmd.find('\\') == std::string::npos, "no backslash (sh would eat it)");
    CHECK(cmd.find('$') == std::string::npos, "no dollar sign visible to sh");
    std::printf("    (command length: %zu bytes)\n", cmd.size());
}

void printSnapshot(const WiFiRssiClient::WindowsHostWifiSnapshot& s) {
    std::printf("    ok=%d state=%s ssid=%s\n", s.ok ? 1 : 0, s.state.c_str(), s.ssid.c_str());
    std::printf("    adapter_name=%s\n", s.adapterName.c_str());
    std::printf("    adapter_desc=%s\n", s.adapterDesc.c_str());
    std::printf("    adapter_status=%s media_type=%s\n", s.adapterStatus.c_str(), s.mediaType.c_str());
    std::printf("    bssid=%s band=%s channel=%s radio=%s auth=%s cipher=%s\n",
                s.bssid.c_str(), s.band.c_str(), s.channel.c_str(), s.radioType.c_str(),
                s.authentication.c_str(), s.cipher.c_str());
    std::printf("    signal_pct=%d rssi_dbm=%d\n", s.signalPct, s.rssiDbm);
    std::printf("    link_rx_mbps=%d link_tx_mbps=%d link_rx_bps=%lld link_tx_bps=%lld\n",
                s.linkRxMbps, s.linkTxMbps,
                static_cast<long long>(s.linkRxBps), static_cast<long long>(s.linkTxBps));
    std::printf("    rx_bytes=%llu tx_bytes=%llu\n",
                static_cast<unsigned long long>(s.rxBytes),
                static_cast<unsigned long long>(s.txBytes));
    std::printf("    rtt_ms=%d rtt_samples=%d jitter_ms=%.2f tcp_loss_pct=%.2f\n",
                s.rttMs, s.rttSamples, s.jitterMs, s.tcpLossPct);
    std::printf("    ping_target=%s ping_count=%d ping_replies=%d ipv4=%s\n",
                s.pingTarget.c_str(), s.pingCount, s.pingReplies, s.ipv4Connectivity.c_str());
    std::printf("    download_bps=%.1f upload_bps=%.1f\n", s.downloadBps, s.uploadBps);
    std::printf("    rtts=[");
    for (size_t i = 0; i < s.rtts.size(); ++i) {
        std::printf("%s%d", i ? "," : "", s.rtts[i]);
    }
    std::printf("]\n");
}

void testCacheSemantics() {
    std::printf("[cache] peek TTL semantics (no live query)\n");
    WiFiRssiClient::WindowsHostWifiSnapshot s;
    // 进程启动后缓存必为空：maxAge=0 与 maxAge=60 都应返回 false
    CHECK(!WiFiRssiClient::peekWindowsHostWifiSnapshot(s, 0.0), "empty cache, maxAge=0 -> false");
    CHECK(!WiFiRssiClient::peekWindowsHostWifiSnapshot(s, 60.0), "empty cache, maxAge=60 -> false");
}

// ---- 挂死防护回归（真实事故：WSL 里裸 ping 无限期执行堵死主线程）----
void testHangProtection() {
    std::printf("[hang] ping candidate selection + timeout wrapper\n");

    // WSL 分支：任何情况下都不能出现裸 "ping"（Linux 版不认识 -n 计数，
    // 会无限期执行）。.exe 后缀/绝对路径保证拿到 Windows 版。
    const auto wslCands = pingCandidateExes(true);
    CHECK(wslCands.size() == 2, "WSL ping candidates: 2 entries");
    bool anyBarePing = false;
    bool allExe = true;
    for (const auto& c : wslCands) {
        if (c == "ping") anyBarePing = true;
        std::string tail = c.size() >= 4 ? c.substr(c.size() - 4) : "";
        std::transform(tail.begin(), tail.end(), tail.begin(),
                       [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });
        if (tail != ".exe") allExe = false;
    }
    CHECK(!anyBarePing, "WSL candidates contain NO bare 'ping' (regression: infinite ping)");
    CHECK(allExe, "WSL candidates all end with .exe (case-insensitive)");
    CHECK(wslCands[0] == "/mnt/c/Windows/System32/PING.EXE", "WSL first candidate is absolute PING.EXE");

    // 原生 Windows 分支：裸 ping 就是 Windows ping，放第一位
    const auto natCands = pingCandidateExes(false);
    CHECK(natCands.size() == 2 && natCands[0] == "ping", "native first candidate is bare 'ping'");

    // timeout 包裹层：快命令照常返回输出（包不包都能过）
    const std::string fast = runCommandCapture("echo NETPULSE_OK hello", /*timeoutMs=*/2000);
    CHECK(fast.find("NETPULSE_OK hello") != std::string::npos,
          "runCommandCapture: fast command output intact (timeout wrapper transparent)");

    // timeout 包裹层：挂死的命令必须在几秒内被杀死并返回空输出。
    // （没有 GNU timeout 的环境跳过——那种环境下这一层本来就不生效。）
    if (haveGnuTimeout()) {
        const auto t0 = std::chrono::steady_clock::now();
        const std::string hung = runCommandCapture("sleep 30", /*timeoutMs=*/1000);
        const double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        CHECK(hung.empty(), "hung command: no output after kill");
        CHECK(el < 5.0, "hung command killed within 5s (not 30s)");
        std::printf("    (hung 'sleep 30' with timeoutMs=1000 returned in %.2fs)\n", el);
    } else {
        std::printf("  [SKIP] no GNU timeout in this environment\n");
    }
}

void testLiveBridge() {
    std::printf("[live] queryWindowsHostWifiSnapshot (WSL gate active: %s)\n",
                isWslEnvironment() ? "yes" : "no (set WSL_DISTRO_NAME=1 to force)");
    if (!isWslEnvironment()) {
        std::printf("  [SKIP] not a WSL environment; nothing to bridge\n");
        return;
    }

    // 冷缓存有界查询：服务端启动路径的真实形态——主线程等一个有上限的时间，
    // 慢活（netsh+ping+powershell）在工作线程里跑。曾经这里无限期等一个
    // WSL 里选错的 ping，主线程卡死 → D-Bus 不应答 → 所有工具调用超时。
    {
        const auto t0 = std::chrono::steady_clock::now();
        WiFiRssiClient::WindowsHostWifiSnapshot sc;
        const bool okc = WiFiRssiClient::queryWindowsHostWifiSnapshotBounded(sc, /*maxWaitSec=*/15.0);
        const double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        CHECK(okc, "cold bounded query ok (worker thread did the slow work)");
        CHECK(el < 15.0, "cold bounded query respected its deadline");
        std::printf("    (cold bounded query returned in %.2fs)\n", el);
        if (okc) {
            printSnapshot(sc);
            CHECK(sc.state == "connected" || sc.state == "disconnected", "cold snapshot state normalized");
        }
    }

    WiFiRssiClient::WindowsHostWifiSnapshot s;
    const bool ok = WiFiRssiClient::queryWindowsHostWifiSnapshot(s);
    CHECK(ok, "queryWindowsHostWifiSnapshot returned ok");
    if (!ok) {
        std::printf("  (bridge unavailable: no WiFi adapter / WLAN service off / "
                    "netsh or powershell.exe missing / WSL interop broken)\n");
        return;
    }
    printSnapshot(s);
    CHECK(s.state == "connected" || s.state == "disconnected", "state normalized");
    if (s.state == "connected") {
        CHECK(!s.ssid.empty(), "ssid non-empty when connected");
        CHECK(s.signalPct > 0, "signal_pct > 0 when connected");
        CHECK(s.rssiDbm >= -100 && s.rssiDbm <= 0, "rssi_dbm in [-100, 0]");
        CHECK(s.rtts.size() > 0, "rtt samples > 0");
    }
    CHECK(s.pingCount == 4, "ping_count == 4");
    CHECK(s.tcpLossPct >= 0.0 && s.tcpLossPct <= 100.0, "tcp_loss_pct in [0, 100]");
    CHECK(s.rttMs >= -1, "rtt_ms >= -1");

    // 缓存语义：刚查询完，peek(60) 必中、peek(0) 必不中
    WiFiRssiClient::WindowsHostWifiSnapshot peeked;
    CHECK(WiFiRssiClient::peekWindowsHostWifiSnapshot(peeked, 60.0), "peek(60) after query -> true");
    CHECK(peeked.ssid == s.ssid, "peeked snapshot matches");
    CHECK(!WiFiRssiClient::peekWindowsHostWifiSnapshot(peeked, 0.0), "peek(0) -> false");

    // 第二次查询（间隔 < 2s 突发合并窗口）：应直接返回缓存，
    // 表现为字节计数器与 RTT 样本原样（没有重新采样）
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    WiFiRssiClient::WindowsHostWifiSnapshot s2;
    const bool ok2 = WiFiRssiClient::queryWindowsHostWifiSnapshot(s2);
    CHECK(ok2, "second query ok (burst-merged)");
    if (ok2) {
        CHECK(s2.rxBytes == s.rxBytes, "second query served from cache (no re-sample)");
        CHECK(s2.ssid == s.ssid, "ssid stable");
    }

    // 第三次查询（间隔 > 2s，真采样）：速率应按字节计数器差值算出。
    // 机器可能真在闲置，所以按"计数器是否变化"分情况断言。
    std::this_thread::sleep_for(std::chrono::milliseconds(2500));
    WiFiRssiClient::WindowsHostWifiSnapshot s3;
    const bool ok3 = WiFiRssiClient::queryWindowsHostWifiSnapshot(s3);
    CHECK(ok3, "third query ok (fresh sample)");
    if (ok3) {
        printSnapshot(s3);
        if (s3.rxBytes != s.rxBytes) {
            CHECK(s3.downloadBps > 0.0, "download rate from byte delta");
        } else {
            CHECK(s3.downloadBps == 0.0, "idle link -> download rate 0");
        }
        if (s3.txBytes != s.txBytes) {
            CHECK(s3.uploadBps > 0.0, "upload rate from byte delta");
        } else {
            CHECK(s3.uploadBps == 0.0, "no upload -> upload rate 0");
        }
    }

    // 缓存新鲜时的有界查询：必须立即返回（5s 复用窗口，不起任何外部命令）
    {
        const auto t0 = std::chrono::steady_clock::now();
        WiFiRssiClient::WindowsHostWifiSnapshot sb;
        const bool okb = WiFiRssiClient::queryWindowsHostWifiSnapshotBounded(sb, 15.0);
        const double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        CHECK(okb, "warm bounded query ok");
        CHECK(el < 3.0, "warm bounded query served from cache (<3s, no external commands)");
        if (okb) CHECK(sb.ssid == s.ssid, "warm snapshot ssid matches");
        std::printf("    (warm bounded query returned in %.3fs)\n", el);
    }
}

}  // namespace

int main() {
    testBase64Vectors();
    testBase64RoundTrip();
    testUtf8ToUtf16Le();
    testNetshChineseGolden();
    testNetshEnglish();
    testNetshDisconnectedAndEmpty();
    testPingParse();
    testAdapterStatsCommandLength();
    testCacheSemantics();
    testHangProtection();
    testLiveBridge();

    if (g_failures == 0) {
        std::printf("\nALL TESTS PASSED\n");
        return 0;
    }
    std::printf("\n%d CHECK(S) FAILED\n", g_failures);
    return 1;
}
