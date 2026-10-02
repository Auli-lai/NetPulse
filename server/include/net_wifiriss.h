#pragma once

#include <string>
#include <memory>
#include <mutex>

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
