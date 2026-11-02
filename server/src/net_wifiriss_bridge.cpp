// net_wifiriss_bridge.cpp
// WSL 宿主机 WiFi 桥接：从 Windows 侧读取真实 WiFi 指标，注入 Linux 服务端。
//
// 【为什么单独一个文件】
// net_wifiriss.cpp 是 wpa_supplicant 客户端（UNIX socket，只在 Linux 有意义）；
// 本文件只依赖 popen + 标准库，没有任何 UNIX socket 依赖，可以在任何平台
// 独立编译、独立测试（tests/ 下有对着真实中文 Windows 验证的 harness）。
// 两边的衔接点是 net_wifiriss.h 的 WindowsHostWifiSnapshot。
//
// 【三路直连，以及为什么不走「一个 PowerShell 脚本包打天下」】
// 字段集与 agent/host_bridge.py 逐一对齐（同一条数据链路的两个实现，改一边
// 必须同步改另一边），但调用机制刻意不同，原因是 C++ 侧的两个硬约束：
//
//   1. **popen 走 sh -c，命令行上限 8191 字符**（cmd.exe 的经典限制，实测：
//      8066 通过 / 10734 报「命令行太长」）。把整包脚本 Base64(UTF-16LE) 塞进
//      -EncodedCommand 需要 ~17KB，直接爆掉——Python 侧用 subprocess 直传
//      argv 不过 sh，所以没这个问题；C++ 必须换路子。
//
//   2. **PowerShell 管道会把原生命令的 UTF-8 输出按 ANSI 代码页解一遍**
//      （中文 Windows 上 netsh 实际吐 UTF-8，一解就乱且不报错）。Python 侧靠
//      StandardOutputEncoding 多编码重试绕开；C++ 侧更干脆——**不经过
//      PowerShell**，popen 直连 netsh，拿到的就是原始 UTF-8 字节，
//      用源码里的 UTF-8 字面量做字节级匹配即可，整个编码问题不存在。
//
// 所以最终形态是三路各干各擅长的事：
//
//   路 1  netsh wlan show interfaces（直连，原始 UTF-8 字节）
//        → 全部 WiFi 专属字段：SSID/BSSID/波段/通道/无线电类型/认证/密码/
//          接收·传输速率/信号百分比。ping 那一路同理：本地化标签无所谓，
//          要的数字（时间=NNms、TTL）全是 ASCII。
//   路 2  ping -n <target>（直连，原始字节）
//        → RTT 样本序列 + 丢包率。
//   路 3  powershell.exe -EncodedCommand <短 ASCII 脚本>（~600 字符，
//        编码后命令行 ~1.7KB，远离 8191 上限）
//        → netsh 给不了的宿主机网卡字节计数器（速率计算需要）、链路速率、
//          IPv4 连通性。脚本本身零非 ASCII 字面量，中文网卡名作为字符串
//          数据流经 UTF8.GetBytes → base64，不出错；输出只有 base64 ASCII，
//          同样免疫管道代码页。
//
// 任何一路失败都只损失对应字段（路 3 失败→速率为 0），不影响其余数据。
//
// 【挂死防护：三层】
// 这个桥接的全部输入都来自外部进程，而它最要命的调用方是**主线程的启动路径**
// （server.cpp 在进入 D-Bus 分发循环前同步调 collectCurrentInterfaces）。
// 早期版本在这里无限期等一个外部进程，结果 WSL 里裸 `ping` 把 `-n` 当成
// "不反查 DNS"、没有 `-c` 就永远不停——主线程卡死，D-Bus 不应答，
// 用户侧表现是「所有工具调用超时」。为此有三层防护：
//   1. ping 候选只认 Windows 版（.exe 后缀 / 绝对路径），从源头排除无限 ping；
//   2. 每条命令用 GNU timeout 包一层（挂了 → N 秒后带着空输出失败）；
//   3. 真正的查询永远甩到工作线程，调用方只等一个有上限的时间
//      （queryWindowsHostWifiSnapshotBounded），等不到就跳过本轮，
//      由后台监控线程的下个周期补上——服务本身永远不应答不过来。

#include "net_wifiriss.h"

#include <sys/stat.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

// ---- 环境判断：只有 WSL 里才存在"Linux 看不到 Windows WiFi"的问题 ----
bool isWslEnvironment() {
    if (const char* distro = std::getenv("WSL_DISTRO_NAME")) {
        if (*distro) return true;
    }
    if (const char* interop = std::getenv("WSL_INTEROP")) {
        if (*interop) return true;
    }
    FILE* fp = std::fopen("/proc/version", "r");
    if (fp) {
        char buf[512] = {0};
        if (std::fgets(buf, sizeof(buf), fp)) {
            std::string v(buf);
            std::transform(v.begin(), v.end(), v.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            std::fclose(fp);
            if (v.find("microsoft") != std::string::npos) return true;
            if (v.find("wsl") != std::string::npos) return true;
        } else {
            std::fclose(fp);
        }
    }
    struct stat st{};
    return ::stat("/proc/sys/fs/binfmt_misc/WSLInterop", &st) == 0;
}

// ---- Base64（-EncodedCommand 的入参、NETPULSE_OK 的出参都要用）----
// 成功标记行。用常量而不是字面量散落各处，避免 substr 偏移量数错字符个数
// （"NETPULSE_OK " 是 12 个字符，曾因硬编码 pos+13 啃掉 base64 首字符）。
const std::string kOkMarker = "NETPULSE_OK ";

const char kBase64Chars[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string base64Encode(const uint8_t* data, size_t len) {
    std::string out;
    out.reserve(((len + 2) / 3) * 4);
    size_t i = 0;
    for (; i + 2 < len; i += 3) {
        const uint32_t n = (static_cast<uint32_t>(data[i]) << 16) |
                           (static_cast<uint32_t>(data[i + 1]) << 8) |
                           static_cast<uint32_t>(data[i + 2]);
        out.push_back(kBase64Chars[(n >> 18) & 63]);
        out.push_back(kBase64Chars[(n >> 12) & 63]);
        out.push_back(kBase64Chars[(n >> 6) & 63]);
        out.push_back(kBase64Chars[n & 63]);
    }
    if (i < len) {
        const bool two = (i + 1 < len);
        uint32_t n = static_cast<uint32_t>(data[i]) << 16;
        if (two) n |= static_cast<uint32_t>(data[i + 1]) << 8;
        out.push_back(kBase64Chars[(n >> 18) & 63]);
        out.push_back(kBase64Chars[(n >> 12) & 63]);
        if (two) {
            out.push_back(kBase64Chars[(n >> 6) & 63]);
            out.push_back('=');
        } else {
            out.push_back('=');
            out.push_back('=');
        }
    }
    return out;
}

int base64Value(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

// 跳过空白与填充符；遇非法字符返回 false
bool base64Decode(const std::string& in, std::string& out) {
    out.clear();
    uint32_t buf = 0;
    int bits = 0;
    for (char c : in) {
        if (c == '=' || std::isspace(static_cast<unsigned char>(c))) continue;
        const int v = base64Value(c);
        if (v < 0) return false;
        buf = (buf << 6) | static_cast<uint32_t>(v);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out.push_back(static_cast<char>((buf >> bits) & 0xFF));
        }
    }
    return true;
}

// ---- UTF-8 → UTF-16LE（-EncodedCommand 的编码要求）----
// 不依赖 Windows API（MultiByteToWideChar 在 Linux 上没有），手写足够。
// 输入来自编译期字符串常量，总是合法 UTF-8；非法字节按 U+FFFD 兜底。
std::vector<uint8_t> utf8ToUtf16Le(const std::string& s) {
    std::vector<uint8_t> out;
    out.reserve(s.size() * 2 + 2);
    size_t i = 0;
    while (i < s.size()) {
        uint32_t cp = 0;
        int extra = 0;
        const auto c = static_cast<unsigned char>(s[i]);
        if (c < 0x80) {
            cp = c;
        } else if ((c & 0xE0) == 0xC0) {
            cp = c & 0x1Fu;
            extra = 1;
        } else if ((c & 0xF0) == 0xE0) {
            cp = c & 0x0Fu;
            extra = 2;
        } else if ((c & 0xF8) == 0xF0) {
            cp = c & 0x07u;
            extra = 3;
        } else {
            cp = 0xFFFD;  // 孤立的后续字节或非法前导字节
        }
        // extra > 0 时上面的分支已确认 c 是合法前导字节，逐个校验后续字节
        bool ok = true;
        for (int k = 0; k < extra; ++k) {
            if (i + 1 + static_cast<size_t>(k) >= s.size()) {
                ok = false;
                break;
            }
            const auto cc = static_cast<unsigned char>(s[i + 1 + static_cast<size_t>(k)]);
            if ((cc & 0xC0) != 0x80) {
                ok = false;
                break;
            }
            cp = (cp << 6) | (cc & 0x3Fu);
        }
        if (!ok) {
            cp = 0xFFFD;
            extra = 0;  // 只跳过当前这个坏字节
        }
        i += static_cast<size_t>(extra) + 1;
        if (cp >= 0x10000) {
            cp -= 0x10000;
            const uint16_t hi = static_cast<uint16_t>(0xD800 + (cp >> 10));
            const uint16_t lo = static_cast<uint16_t>(0xDC00 + (cp & 0x3FF));
            out.push_back(static_cast<uint8_t>(hi & 0xFF));
            out.push_back(static_cast<uint8_t>(hi >> 8));
            out.push_back(static_cast<uint8_t>(lo & 0xFF));
            out.push_back(static_cast<uint8_t>(lo >> 8));
        } else {
            const uint16_t u = static_cast<uint16_t>(cp);
            out.push_back(static_cast<uint8_t>(u & 0xFF));
            out.push_back(static_cast<uint8_t>(u >> 8));
        }
    }
    return out;
}

// ---- 执行外部命令（popen：WSL interop 下可直接启动 Windows 可执行文件）----
// stderr 合并进 stdout：桥接失败时第一手错误信息对排障至关重要，
// 而调用方本来就按行查找 NETPULSE_OK 标记，混入的报错行不影响解析。
// 注意：命令字符串会经 sh 解析，**不能出现反斜杠**（会被当转义符吃掉）——
// Windows 路径一律用正斜杠（PowerShell/netsh/ping 都接受）。
std::string popenCapture(const std::string& cmd) {
    std::string output;
    // 注意：不加 std:: 限定——Linux 的 popen 在全局命名空间，
    // MinGW 也只认全局的 popen/_popen（加 std:: 两边都编不过）。
    FILE* fp = popen(cmd.c_str(), "r");
    if (!fp) return output;
    char buf[4096];
    while (std::fgets(buf, sizeof(buf), fp) != nullptr) {
        output += buf;
    }
    pclose(fp);
    return output;
}

// 有没有 GNU coreutils 的 timeout（WSL / Git Bash 都有；原生 Windows 没有）。
// 探测一次就够：GNU timeout 会打印版本行，cmd 的 timeout 和"不存在"都会快速失败。
bool haveGnuTimeout() {
    static const bool ok =
        popenCapture("timeout --version 2>&1").find("GNU coreutils") != std::string::npos;
    return ok;
}

// timeoutMs > 0 且环境有 GNU timeout 时，把命令包一层：
// WSL interop 的 Windows 子进程偶尔会永远不退出（会话注销、PowerShell 卡
// 启动、被杀软按住……）。没有这层保护，一次挂死就够堵掉主线程的启动路径，
// 整个 D-Bus 服务不应答。包一层之后，挂死变成 N 秒后带着空输出失败，
// 各路解析自然降级（速率为 0 / RTT 为 -1），服务照常运转。
std::string runCommandCapture(const std::string& cmd, int timeoutMs = 0) {
    if (timeoutMs > 0 && haveGnuTimeout()) {
        const int sec = (timeoutMs + 999) / 1000;
        return popenCapture("timeout -s KILL " + std::to_string(sec) + " " + cmd);
    }
    return popenCapture(cmd);
}

// ---- 把探测目标安全地烘进参数（防注入：只放行 hostname/IP 安全字符）----
std::string sanitizePingTarget(const std::string& raw) {
    std::string t;
    for (char c : raw) {
        if (std::isalnum(static_cast<unsigned char>(c)) || c == '.' || c == '-' || c == ':') {
            t.push_back(c);
        }
    }
    return t.empty() ? std::string("223.5.5.5") : t;
}

// ---- ping 可执行文件候选（顺序即优先级）----
// WSL：只认 Windows 版 ping。裸 "ping" 在 WSL 里是 Linux 版，会把 -n 当
// "不反查 DNS" 从而无限期执行（真实事故见路 2 的注释），因此 WSL 分支
// **任何情况下都不能出现裸 "ping"**——单测钉死这个不变量。
std::vector<std::string> pingCandidateExes(bool wsl) {
    if (wsl) {
        return {"/mnt/c/Windows/System32/PING.EXE", "ping.exe"};
    }
    return {"ping", "ping.exe"};
}

// ---- key=value 解析（PowerShell 那一路的 payload 用）----
std::vector<std::pair<std::string, std::string>> parseKeyValues(const std::string& text) {
    std::vector<std::pair<std::string, std::string>> kv;
    std::istringstream iss(text);
    std::string line;
    while (std::getline(iss, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        const auto eq = line.find('=');
        if (eq == std::string::npos) continue;
        kv.emplace_back(line.substr(0, eq), line.substr(eq + 1));
    }
    return kv;
}

const std::string* kvFind(const std::vector<std::pair<std::string, std::string>>& kv,
                          const std::string& key) {
    for (const auto& p : kv) {
        if (p.first == key) return &p.second;
    }
    return nullptr;
}

std::string kvStr(const std::vector<std::pair<std::string, std::string>>& kv, const std::string& key) {
    const std::string* v = kvFind(kv, key);
    return v ? *v : std::string();
}

int64_t kvInt64(const std::vector<std::pair<std::string, std::string>>& kv, const std::string& key,
                int64_t fallback) {
    const std::string* v = kvFind(kv, key);
    if (!v || v->empty()) return fallback;
    try {
        return std::stoll(*v);
    } catch (...) {
        return fallback;
    }
}

// ---- 通用小工具 ----
std::string toLowerAscii(std::string s) {
    for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

std::string trimAscii(const std::string& s) {
    size_t b = 0, e = s.size();
    while (b < e && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
    return s.substr(b, e - b);
}

// 去掉所有空白（ netsh 标签的填充空格在不同系统/语言下不一致，必须归一化）
std::string stripSpaces(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        if (!std::isspace(static_cast<unsigned char>(c))) out.push_back(c);
    }
    return out;
}

// ---- netsh 输出解析 ----
// netsh wlan show interfaces 重定向到管道时输出 **UTF-8 字节**（实测：中文
// Windows 上"信号"是 E4 BF A1 E5 8F B7），所以这里直接字节级匹配：源码里的
// UTF-8 字面量原样编进二进制，不需要任何解码。
//
// 行格式（真实样本，注意填充空格与全角冒号都不规整）：
//     名称                   : WLAN
//     状态                  : 已连接
//     信号                   : 71%
//     接收速率(Mbps)         : 310
//     传输速率 (Mbps)        : 293
//     已配置     QoS MSCS： 0        <- 全角冒号 + 顶格，不关心的行直接跳过
struct NetshFields {
    bool any = false;  // 是否匹配到过任何已知字段（netsh 本身失败时为 false）
    std::string name, desc, state, ssid, bssid, band, channel, radio, auth, cipher;
    int rxMbps = -1, txMbps = -1, signalPct = 0;
};

bool labelIs(const std::string& stripped, const std::vector<const char*>& candidates) {
    for (const char* c : candidates) {
        if (stripped == c) return true;
    }
    return false;
}

NetshFields parseNetshInterfaces(const std::string& text) {
    NetshFields f;
    std::istringstream iss(text);
    std::string line;
    while (std::getline(iss, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();

        // 找第一个冒号：ASCII ':' 或全角 '：'（EF BC 9A，netsh 的 QoS 行用它）
        size_t colon = std::string::npos;
        size_t colonWidth = 1;
        for (size_t i = 0; i < line.size(); ++i) {
            if (line[i] == ':') { colon = i; colonWidth = 1; break; }
            if (i + 2 < line.size() &&
                static_cast<unsigned char>(line[i]) == 0xEF &&
                static_cast<unsigned char>(line[i + 1]) == 0xBC &&
                static_cast<unsigned char>(line[i + 2]) == 0x9A) {
                colon = i;
                colonWidth = 3;
                break;
            }
        }
        if (colon == std::string::npos) continue;

        const std::string label = stripSpaces(line.substr(0, colon));
        const std::string value = trimAscii(line.substr(colon + colonWidth));

        // 候选标签全部是"去空白后"的形态；中文为 UTF-8 字面量，英文为小写无关的
        // 原样（netsh 英文输出标签大小写固定）。多语言变体穷举到常见繁体。
        if (labelIs(label, {"名称", "接口名称", "Name", "名稱"})) {
            f.name = value;
            f.any = true;
        } else if (labelIs(label, {"说明", "描述", "Description"})) {
            f.desc = value;
            f.any = true;
        } else if (labelIs(label, {"状态", "State", "狀態"})) {
            f.state = value;
            f.any = true;
        } else if (labelIs(label, {"SSID"})) {
            f.ssid = value;
            f.any = true;
        } else if (labelIs(label, {"APBSSID", "BSSID"})) {  // "AP BSSID" 去空白后
            f.bssid = value;
            f.any = true;
        } else if (labelIs(label, {"波段", "频段", "頻帶", "Band", "頻段"})) {
            f.band = value;
            f.any = true;
        } else if (labelIs(label, {"通道", "信道", "頻道", "Channel", "波道"})) {
            f.channel = value;
            f.any = true;
        } else if (labelIs(label, {"无线电类型", "無線類型", "Radiotype", "Wirelesstype"})) {
            f.radio = value;
            f.any = true;
        } else if (labelIs(label, {"身份验证", "验证", "驗證", "Authentication"})) {
            f.auth = value;
            f.any = true;
        } else if (labelIs(label, {"密码", "加密", "Cipher", "Encryption"})) {
            f.cipher = value;
            f.any = true;
        } else if (labelIs(label, {"接收速率(Mbps)", "Receiverate(Mbps)"})) {
            f.rxMbps = std::atoi(value.c_str());  // "310"；异常时为 0
            f.any = true;
        } else if (labelIs(label, {"传输速率(Mbps)", "Transmitrate(Mbps)"})) {
            f.txMbps = std::atoi(value.c_str());
            f.any = true;
        } else if (labelIs(label, {"信号", "Signal", "電波強度", "Signalstrength"})) {
            f.signalPct = std::atoi(value.c_str());  // "71%" -> 71
            f.any = true;
        }
    }
    return f;
}

// ---- ping 输出解析 ----
// 中文 Windows 的 ping 走 OEM 代码页（GBK），英文走 ASCII，但两边的
// "TTL=" 和 "NNms" 都是 ASCII——只按 ASCII 模式切，本地化标签一律无视。
// 只统计含 TTL 的行（回复行）；汇总行（"最短/最长/平均"或
// "Minimum/Maximum/Average"）不含 TTL，天然被排除。
struct PingResult {
    std::vector<int> rtts;
};

PingResult parsePingOutput(const std::string& text) {
    PingResult r;
    std::istringstream iss(text);
    std::string line;
    while (std::getline(iss, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.find("TTL=") == std::string::npos &&
            line.find("ttl=") == std::string::npos) {
            continue;
        }
        // 找 "<digits>ms"（容忍 "<1ms" 这种亚毫秒表示）
        for (size_t i = 0; i + 1 < line.size(); ++i) {
            if (line[i] != 'm' && line[i] != 'M') continue;
            if (line[i + 1] != 's' && line[i + 1] != 'S') continue;
            // 从 'm' 往前跳空白，再跳可选的 '<'，再收数字
            size_t j = i;
            while (j > 0 && std::isspace(static_cast<unsigned char>(line[j - 1]))) --j;
            const bool subMs = (j > 0 && line[j - 1] == '<');
            if (subMs) --j;
            size_t end = j;
            while (j > 0 && std::isdigit(static_cast<unsigned char>(line[j - 1]))) --j;
            if (j == end) continue;  // 'm' 前面不是数字（如 "TTL=48ms" 之外的噪声）
            const int v = std::atoi(line.substr(j, end - j).c_str());
            r.rtts.push_back(subMs && v == 0 ? 1 : v);
            break;  // 一行只取一个 RTT
        }
    }
    return r;
}

// ---- 状态归一化（字节 contains，不解码）----
// 注意顺序：必须先判断开——英文 "disconnected" 包含子串 "connected"，
// 先查 connected 会把断开态误判成已连接（实测踩中，测试钉死）。
std::string normalizeWifiState(const std::string& netshState, const std::string& adStatus,
                               const std::string& ssid) {
    const std::string low = toLowerAscii(netshState);
    if (low.find("disconnected") != std::string::npos ||
        netshState.find("已断开") != std::string::npos ||
        netshState.find("断开") != std::string::npos ||
        netshState.find("斷開") != std::string::npos) {
        return "disconnected";
    }
    if (low.find("connected") != std::string::npos ||
        netshState.find("已连接") != std::string::npos ||
        netshState.find("已連線") != std::string::npos) {
        return "connected";
    }
    // netsh 没给可用状态时用 Get-NetAdapter.Status 兜底
    if (adStatus == "Up" && !ssid.empty()) return "connected";
    if (adStatus == "Disconnected") return "disconnected";
    return "unknown";
}

// ---- PowerShell 短脚本：netsh 给不了的宿主机网卡字节计数器 ----
// 零非 ASCII 字面量：中文网卡名/描述是**字符串数据**，流经
// UTF8.GetBytes → base64，不受代码页影响；输出同样只有 base64 ASCII。
// 长度 ~600 字符 → UTF-16 → base64 后整条命令行 ~1.7KB，远离 8191 上限。
const char kAdapterStatsScript[] = R"PS(
$ErrorActionPreference='SilentlyContinue'
$n=(Get-NetAdapter | ? { $_.MediaType -eq 'Native 802.11' } | Select-Object -First 1).Name
$s=Get-NetAdapterStatistics -Name $n
$a=Get-NetAdapter -Name $n
$t=@()
$t+=('rx_bytes=' + $s.ReceivedBytes)
$t+=('tx_bytes=' + $s.SentBytes)
$t+=('link_rx_bps=' + $a.ReceiveLinkSpeed)
$t+=('link_tx_bps=' + $a.TransmitLinkSpeed)
$t+=('status=' + $a.Status)
$t+=('media=' + $a.MediaType)
$t+=('ipv4=' + (Get-NetConnectionProfile -InterfaceAlias $n).IPv4Connectivity)
[Console]::WriteLine('NETPULSE_OK ' + [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(($t -join "`r`n"))))
)PS";

std::string buildAdapterStatsCommand(const char* powershellExe) {
    const std::vector<uint8_t> u16 = utf8ToUtf16Le(kAdapterStatsScript);
    const std::string encoded = base64Encode(u16.data(), u16.size());
    return std::string(powershellExe) +
           " -NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded;
}

// ---- 跨采样的速率状态（与 Python 版 _apply_rates 同逻辑）----
struct HostWifiRateState {
    bool has = false;
    uint64_t rxBytes = 0;
    uint64_t txBytes = 0;
    double timeSec = 0.0;
    double jitterMs = 0.0;
};

std::mutex g_hostWifiStateMutex;
HostWifiRateState g_hostWifiState;

// ---- 快照缓存：合并突发查询 + 供 collectCurrentInterfaces 无阻塞注入 ----
struct SnapshotCache {
    std::mutex mu;
    bool valid = false;
    WiFiRssiClient::WindowsHostWifiSnapshot snap;
    std::chrono::steady_clock::time_point at;
};

SnapshotCache g_cache;

// ---- 有界查询的门控：真正查询全局同时最多一个，且永远在工作线程里跑 ----
// queryWindowsHostWifiSnapshot 要 3~6 秒（netsh + 4 个 ping + PowerShell
// 冷启动），而它的调用方包括**主线程的启动路径**（server.cpp 进 D-Bus
// 分发循环前同步调 collectCurrentInterfaces）。无限期同步等它，一旦
// Windows 子进程挂住，主线程卡死 → D-Bus 不应答 → 所有工具调用超时。
// 现在：慢查询甩到 detached 工作线程，调用方只等一个有上限的时间；
// 工作线程挂住也只损失它自己（inFlight 保持 true，后续调用快速失败，
// 不会线程泄漏；超过硬上限 60 秒后放行重试，换取不重启就能恢复）。
struct QueryGate {
    std::mutex mu;
    std::condition_variable cv;
    bool inFlight = false;
    uint64_t seq = 0;  // 每次真实查询结束（成败都算）+1；等待方靠它判断"有新结果了"
    std::chrono::steady_clock::time_point hardDeadline;  // 在飞查询的最迟完成时刻
};

QueryGate g_gate;

// 在飞查询的硬上限：三条命令的 timeout 合计 6+8+15=29s，留足余量。
const int kQueryHardMs = 45000;

}  // namespace

bool WiFiRssiClient::peekWindowsHostWifiSnapshot(WindowsHostWifiSnapshot& out, double maxAgeSec) {
    std::lock_guard<std::mutex> lk(g_cache.mu);
    if (!g_cache.valid) return false;
    const double age =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - g_cache.at).count();
    if (age >= maxAgeSec) return false;
    out = g_cache.snap;
    return out.ok;
}

bool WiFiRssiClient::queryWindowsHostWifiSnapshotBounded(WindowsHostWifiSnapshot& out,
                                                         double maxWaitSec) {
    out = WindowsHostWifiSnapshot{};
    if (!isWslEnvironment()) return false;

    const auto now = std::chrono::steady_clock::now();

    // 缓存够新就直接用：rssi（10s）/ iface（10s）两个监控线程错峰触发，
    // 5 秒复用窗口把实际查询频率压到≈每 10s 一次，不会互相踩。
    {
        std::lock_guard<std::mutex> lk(g_cache.mu);
        if (g_cache.valid) {
            const double age = std::chrono::duration<double>(now - g_cache.at).count();
            if (age < 5.0) {
                out = g_cache.snap;
                return out.ok;
            }
        }
    }

    // 没有在飞的查询就起一个；有就搭便车（等它结束）。
    uint64_t waitedSeq = 0;
    {
        std::lock_guard<std::mutex> lk(g_gate.mu);
        waitedSeq = g_gate.seq;
        const bool stale = g_gate.inFlight &&
                           now > g_gate.hardDeadline + std::chrono::seconds(60);
        if (!g_gate.inFlight || stale) {
            if (stale) {
                std::fprintf(stderr,
                             "[wifi] previous bridge query still stuck after %ds; "
                             "starting a fresh one (old worker thread leaked)\n",
                             kQueryHardMs / 1000 + 60);
            }
            g_gate.inFlight = true;
            g_gate.hardDeadline = now + std::chrono::milliseconds(kQueryHardMs);
            std::thread([] {
                WindowsHostWifiSnapshot s;
                queryWindowsHostWifiSnapshot(s);  // 慢全在这里；挂住也只挂这个线程
                {
                    std::lock_guard<std::mutex> lk(g_cache.mu);
                    g_cache.snap = s;
                    g_cache.at = std::chrono::steady_clock::now();
                    g_cache.valid = true;
                }
                {
                    std::lock_guard<std::mutex> lk(g_gate.mu);
                    g_gate.inFlight = false;
                    g_gate.seq++;
                    g_gate.cv.notify_all();
                }
            }).detach();
        }
    }

    // 有界等待：自己的上限和在飞查询的硬上限取早者。
    {
        std::unique_lock<std::mutex> lk(g_gate.mu);
        auto until = now + std::chrono::milliseconds(static_cast<int>(maxWaitSec * 1000));
        if (g_gate.hardDeadline < until) until = g_gate.hardDeadline;
        g_gate.cv.wait_until(lk, until, [&] { return g_gate.seq != waitedSeq; });
    }

    {
        std::lock_guard<std::mutex> lk(g_cache.mu);
        if (g_cache.valid) {
            out = g_cache.snap;
            return out.ok;
        }
    }
    return false;
}

bool WiFiRssiClient::queryWindowsHostWifiSnapshot(WindowsHostWifiSnapshot& out) {
    out = WindowsHostWifiSnapshot{};

    // 原生 Linux 没有"Windows 宿主"可言——直接跳过，别白起进程。
    // （WSL 里 Linux 有 eth0 但看不到 Windows WiFi，这才是桥接存在的理由。）
    if (!isWslEnvironment()) return false;

    // 突发合并：rssi / iface 两个监控线程都可能触发查询，2 秒内的重复调用
    // 直接复用上次结果，避免两个 3~4 秒的 ping 互相踩踏、互相拖慢。
    {
        std::lock_guard<std::mutex> lk(g_cache.mu);
        if (g_cache.valid) {
            const double age =
                std::chrono::duration<double>(std::chrono::steady_clock::now() - g_cache.at).count();
            if (age < 2.0) {
                out = g_cache.snap;
                return out.ok;
            }
        }
    }

    // ---- 路 1：netsh，直连接取原始 UTF-8 字节 ----
    NetshFields nf;
    std::string netshRaw;
    const char* netshCandidates[] = {
        "netsh",
        "/mnt/c/Windows/System32/netsh.exe",  // sudo 剃光 PATH 时的兜底
    };
    for (const char* exe : netshCandidates) {
        netshRaw = runCommandCapture(std::string(exe) + " wlan show interfaces 2>&1",
                                     /*timeoutMs=*/6000);
        nf = parseNetshInterfaces(netshRaw);
        if (nf.any) break;
    }
    if (!nf.any) {
        // netsh 拿不到任何已知字段：无 WiFi 网卡 / WLAN 服务未运行 / 不在 Windows
        if (!netshRaw.empty()) {
            std::cerr << "[wifi] netsh produced no known fields: " << netshRaw.substr(0, 200)
                      << std::endl;
        }
        return false;
    }

    // ---- 路 2：ping，RTT 与丢包 ----
    // ⚠️ WSL 里**绝不能**用裸 `ping`：WSL 的 PATH 里 Linux 的 /usr/bin/ping
    // 优先，它把 Windows 的 `-n`（计数）当成"不反查 DNS"（合法参数），
    // 于是命令变成"无限 ping"，pclose 永远不返回。这不是假想事故——
    // 真实发生过：主线程在启动路径上等它，整个 D-Bus 服务不应答，
    // 用户侧所有工具调用超时。所以 WSL 下只认 .exe 后缀 / 绝对路径的
    // Windows ping（-n 才是计数）；语义也对：桥接要测的是"宿主机
    // Windows 的上网质量"，Linux ping 测的是 WSL eth0 的 NAT 出口，
    // 根本不是同一条路。
    const char* targetEnv = std::getenv("WEAKNET_PING_TARGET");
    const std::string pingTarget = (targetEnv && *targetEnv) ? targetEnv : "223.5.5.5";
    const int pingCount = 4;  // Python 侧用 5；C++ 有两个周期调用方，少一个样本换 1 秒
    PingResult pr;
    for (const std::string& exe : pingCandidateExes(isWslEnvironment())) {
        const std::string cmd = exe + " -n " + std::to_string(pingCount) + " " +
                                sanitizePingTarget(pingTarget) + " 2>&1";
        pr = parsePingOutput(runCommandCapture(cmd, /*timeoutMs=*/8000));
        if (!pr.rtts.empty()) break;
    }

    // ---- 路 3：PowerShell 短脚本，网卡字节计数器 ----
    std::string psAll;
    const char* psCandidates[] = {
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "powershell.exe",
    };
    for (const char* ps : psCandidates) {
        psAll = runCommandCapture(buildAdapterStatsCommand(ps) + " 2>&1",
                                  /*timeoutMs=*/15000);
        if (psAll.find(kOkMarker) != std::string::npos) break;
        if (!psAll.empty()) {
            std::cerr << "[wifi] adapter-stats probe failed via " << ps << ": "
                      << psAll.substr(0, 200) << std::endl;
        }
        psAll.clear();
    }
    std::string payload;
    {
        std::istringstream iss(psAll);
        std::string line;
        while (std::getline(iss, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            const auto pos = line.find(kOkMarker);
            if (pos == std::string::npos) continue;
            if (!base64Decode(line.substr(pos + kOkMarker.size()), payload)) payload.clear();
            break;
        }
    }
    const auto kv = parseKeyValues(payload);

    // ---- 汇总 ----
    out.ok = true;
    out.ssid = nf.ssid;
    out.adapterName = nf.name;
    out.adapterDesc = nf.desc;
    out.bssid = nf.bssid;
    out.band = nf.band;
    out.channel = nf.channel;
    out.radioType = nf.radio;
    out.authentication = nf.auth;
    out.cipher = nf.cipher;
    out.linkRxMbps = nf.rxMbps;
    out.linkTxMbps = nf.txMbps;
    out.signalPct = nf.signalPct;
    // Windows 只给信号百分比，dBm 由 (pct/2)-100 估算（与 Python 侧同公式）
    out.rssiDbm = nf.signalPct > 0 ? static_cast<int>(std::lround(nf.signalPct / 2.0)) - 100 : -1000;

    out.adapterStatus = kvStr(kv, "status");
    out.mediaType = kvStr(kv, "media");
    out.linkRxBps = kvInt64(kv, "link_rx_bps", 0);
    out.linkTxBps = kvInt64(kv, "link_tx_bps", 0);
    out.rxBytes = static_cast<uint64_t>(kvInt64(kv, "rx_bytes", 0));
    out.txBytes = static_cast<uint64_t>(kvInt64(kv, "tx_bytes", 0));
    out.ipv4Connectivity = kvStr(kv, "ipv4");

    out.state = normalizeWifiState(nf.state, out.adapterStatus, out.ssid);
    out.pingTarget = pingTarget;
    out.pingCount = pingCount;
    out.pingReplies = static_cast<int>(pr.rtts.size());
    out.rtts = pr.rtts;
    out.rttSamples = static_cast<int>(pr.rtts.size());
    out.tcpLossPct = pingCount > 0
                         ? (pingCount - static_cast<double>(pr.rtts.size())) * 100.0 / pingCount
                         : 0.0;

    if (!out.rtts.empty()) {
        long sum = 0;
        for (int v : out.rtts) sum += v;
        out.rttMs = static_cast<int>(sum / static_cast<long>(out.rtts.size()));
        if (out.rtts.size() >= 2) {
            double j = 0.0;
            for (size_t i = 1; i < out.rtts.size(); ++i) {
                j += std::abs(out.rtts[i] - out.rtts[i - 1]);
            }
            out.jitterMs = j / static_cast<double>(out.rtts.size() - 1);
        }
    }

    // 速率 = 两次采样的累计字节差 ÷ 时间差（与 Python 版同规则）：
    // dt <= 0.1s 不算（防止缓存/重复调用导致虚高）；计数器重置时只增不算差。
    {
        std::lock_guard<std::mutex> lk(g_hostWifiStateMutex);
        const auto now = std::chrono::steady_clock::now();
        const double nowSec = std::chrono::duration<double>(now.time_since_epoch()).count();
        if (g_hostWifiState.has) {
            const double dt = nowSec - g_hostWifiState.timeSec;
            if (dt > 0.1) {
                if (out.rxBytes >= g_hostWifiState.rxBytes) {
                    out.downloadBps = static_cast<double>(out.rxBytes - g_hostWifiState.rxBytes) / dt;
                }
                if (out.txBytes >= g_hostWifiState.txBytes) {
                    out.uploadBps = static_cast<double>(out.txBytes - g_hostWifiState.txBytes) / dt;
                }
            }
        }
        g_hostWifiState.has = true;
        g_hostWifiState.rxBytes = out.rxBytes;
        g_hostWifiState.txBytes = out.txBytes;
        g_hostWifiState.timeSec = nowSec;
        if (out.rttSamples >= 2) {
            g_hostWifiState.jitterMs = out.jitterMs;
        } else if (out.rttMs < 0) {
            out.jitterMs = 0.0;  // RTT 无效时不要把历史抖动当成当前抖动
        } else {
            out.jitterMs = g_hostWifiState.jitterMs;
        }
    }

    // 写入缓存（peek 与突发合并都读它）
    {
        std::lock_guard<std::mutex> lk(g_cache.mu);
        g_cache.snap = out;
        g_cache.at = std::chrono::steady_clock::now();
        g_cache.valid = true;
    }
    return true;
}

bool WiFiRssiClient::queryWindowsHostWifi(int& rssiDbm, std::string& ssid, std::string& state) {
    WindowsHostWifiSnapshot snapshot;
    if (!queryWindowsHostWifiSnapshot(snapshot)) return false;
    rssiDbm = snapshot.rssiDbm;
    ssid = snapshot.ssid;
    state = snapshot.state;
    return true;
}
