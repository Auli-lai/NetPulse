#pragma once

#include <string>
#include <memory>
#include <mutex>
#include <cstdint>
#include <vector>

// 通过 UNIX DGRAM 与 wpa_supplicant 控制接口通信，查询 RSSI
class WiFiRssiClient {
public:
    WiFiRssiClient();
    ~WiFiRssiClient();

    // 线程安全懒汉单例（可选）
    static std::shared_ptr<WiFiRssiClient> getInstance();

    // 连接到 ctrl 路径（通常为 /var/run/wpa_supplicant）与指定接口（如 wlan0）
    // 成功返回 true；失败返回 false
    bool connect(const std::string& ifaceName, const std::string& ctrlDir = "/var/run/wpa_supplicant");

    // 发送 SIGNAL_POLL 并解析 RSSI，成功返回 rssi(dBm)，失败返回极小值(-1000)
    int getRssi();

    // WSL 桥接：Windows 宿主机 WiFi 的完整指标快照。
    //
    // 字段与 agent/host_bridge.py 的 Python 桥接**逐一对齐**——两边是同一条
    // 数据链路的两个实现（C++ 服务端注入 / Python 客户端合并），字段名是契约，
    // 改一边必须改另一边。
    struct WindowsHostWifiSnapshot {
        bool ok = false;
        std::string ssid;
        std::string state;
        int rssiDbm = -1000;
        int rttMs = -1;
        int rttSamples = 0;
        double jitterMs = 0.0;
        uint64_t rxBytes = 0;
        uint64_t txBytes = 0;
        double downloadBps = 0.0;
        double uploadBps = 0.0;
        int linkRxMbps = -1;
        int linkTxMbps = -1;

        // ---- 扩展字段（对齐 host_bridge.py）----
        std::string adapterName;      // Windows 网卡名（netsh "名称"）
        std::string adapterDesc;      // 网卡描述（如 Intel(R) Wi-Fi 6 AX201）
        std::string adapterStatus;    // Get-NetAdapter.Status（Up/Disconnected）
        std::string mediaType;        // Get-NetAdapter.MediaType（WiFi = Native 802.11）
        std::string bssid;            // AP 的 MAC
        std::string band;             // 频段（2.4/5/6 GHz）
        std::string channel;          // 信道（字符串：个别系统会带非数字）
        std::string radioType;        // 无线电类型（802.11ax/ac/n...）
        std::string authentication;   // 认证方式（WPA2-Personal/开放式...）
        std::string cipher;           // 加密方式
        int signalPct = 0;            // 信号百分比（Windows 只给这个，dBm 是估算）
        int64_t linkRxBps = 0;        // Get-NetAdapter.ReceiveLinkSpeed（bps）
        int64_t linkTxBps = 0;        // Get-NetAdapter.TransmitLinkSpeed（bps）
        double tcpLossPct = 0.0;      // ping 丢包率（百分比）
        std::string ipv4Connectivity; // Get-NetConnectionProfile.IPv4Connectivity
        std::string pingTarget;       // 本次探测用的目标
        int pingCount = 0;
        int pingReplies = 0;
        std::vector<int> rtts;        // 原始 RTT 样本（毫秒）
    };

    // WSL 桥接：查询 Windows 宿主机 WiFi 的完整指标。
    //
    // 实现分三路，全部直连接取原始字节（见 net_wifiriss_bridge.cpp 顶部注释）：
    //   1. netsh wlan show interfaces —— WiFi 专属字段（UTF-8 原始字节，字节级匹配）
    //   2. ping -n <target>            —— RTT/丢包（本地化标签无所谓，只取 ASCII 数字）
    //   3. powershell.exe -EncodedCommand <短 ASCII 脚本> —— 网卡字节计数器（base64 输出）
    static bool queryWindowsHostWifiSnapshot(WindowsHostWifiSnapshot& out);

    // 有界版：与上面同一个查询，但慢活甩到工作线程（全局同时最多一个），
    // 本调用最多等 maxWaitSec 秒；等不到就返回 false（跳过本轮），
    // 由后台监控线程的下个周期补上。
    //
    // **给可能跑在主线程/持锁路径上的调用方用**——尤其是 server.cpp 的
    // 启动序列：它在进入 D-Bus 分发循环前同步调 collectCurrentInterfaces，
    // 在那里无限期等一个外部进程，一旦挂住整个服务就不应答
    // （真实事故：WSL 里 ping 候选选错，无限 ping 堵死主线程）。
    // 缓存 5 秒内的重复调用直接返回，不发任何查询。
    static bool queryWindowsHostWifiSnapshotBounded(WindowsHostWifiSnapshot& out,
                                                    double maxWaitSec = 15.0);

    // 只读缓存：返回 maxAgeSec 内的最近一次快照，不发起任何查询。
    // collectCurrentInterfaces() 每 10s 被调用一次，每次都同步等 3~4 秒的 ping
    // 会拖死监控线程；注入 wlan0 只需要一份合理快照，新鲜数据由 rssi 监控线程的
    // updateWifiRssi 周期刷新。启动后首次（缓存为空）由调用方回落到真查询。
    static bool peekWindowsHostWifiSnapshot(WindowsHostWifiSnapshot& out, double maxAgeSec = 60.0);

    // 兼容旧接口：只查 RSSI/SSID/状态
    static bool queryWindowsHostWifi(int& rssiDbm, std::string& ssid, std::string& state);

private:
    int sockfd_ = -1;
    std::string iface_;
    std::string ctrlDir_;
    std::string localSockPath_;

    static std::once_flag s_onceFlag;
    static std::shared_ptr<WiFiRssiClient> s_instance;

    bool bindLocal();
    bool connectRemote();
    std::string sendCommand(const std::string& cmd);
};
