// net_info.hpp
// 定义 NetInfo 类：存储网络信息与基本比较

#pragma once

#include <cstdint>
#include <string>

namespace weaknet_dbus {

enum class NetType {
    Unknown = 0,
    Ethernet,
    WiFi,
    Cellular,
};

enum class NetState {
    Down = 0,
    Up,
};

enum class LinkQuality {
    Unknown = 0,
    Good,
    Fair,
    Poor,
    Bad
};

class NetInfo {
public:
    NetInfo() = default;
    explicit NetInfo(std::string name) : ifname_(std::move(name)) {}

    // 基本属性
    void setIfName(const std::string& n) { ifname_ = n; }
    const std::string& ifName() const { return ifname_; }

    void setDefaultRoute(bool v) { is_default_ = v; }
    bool isDefaultRoute() const { return is_default_; }

    void setType(NetType t) { type_ = t; }
    NetType type() const { return type_; }

    void setRttMs(int rtt) { rtt_ms_ = rtt; }
    int rttMs() const { return rtt_ms_; }

    void setPrevRttMs(int rtt) { prev_rtt_ms_ = rtt; }
    int prevRttMs() const { return prev_rtt_ms_; }

    void setState(NetState s) { state_ = s; }
    NetState state() const { return state_; }

    void setQuality(LinkQuality q) { quality_ = q; }
    LinkQuality quality() const { return quality_; }

    void setRssiDbm(int rssi) { rssi_dbm_ = rssi; }
    int rssiDbm() const { return rssi_dbm_; }

    void setSsid(const std::string& ssid) { ssid_ = ssid; }
    const std::string& ssid() const { return ssid_; }

    void setJitterMs(double jitter) { jitter_ms_ = jitter; }
    double jitterMs() const { return jitter_ms_; }

    void setRttSamples(int n) { rtt_samples_ = n; }
    int rttSamples() const { return rtt_samples_; }

    void setHasTraffic(bool v) { has_traffic_ = v; }
    bool hasTraffic() const { return has_traffic_; }

    void setDownloadBps(uint64_t bps) { download_bps_ = bps; }
    uint64_t downloadBps() const { return download_bps_; }

    void setUploadBps(uint64_t bps) { upload_bps_ = bps; }
    uint64_t uploadBps() const { return upload_bps_; }

    void setLinkRxMbps(int mbps) { link_rx_mbps_ = mbps; }
    int linkRxMbps() const { return link_rx_mbps_; }

    void setLinkTxMbps(int mbps) { link_tx_mbps_ = mbps; }
    int linkTxMbps() const { return link_tx_mbps_; }

    void setHostWifiSynthetic(bool v) { host_wifi_synthetic_ = v; }
    bool hostWifiSynthetic() const { return host_wifi_synthetic_; }

    void setUsingNow(bool v) { using_now_ = v; }
    bool usingNow() const { return using_now_; }

    // TCP丢包率相关
    void setTcpLossRate(double rate) { tcp_loss_rate_ = rate; }
    double tcpLossRate() const { return tcp_loss_rate_; }
    
    void setTcpLossLevel(const std::string& level) { tcp_loss_level_ = level; }
    const std::string& tcpLossLevel() const { return tcp_loss_level_; }
    
    // 流量统计相关
    void setTrafficStats(uint64_t totalBps, uint64_t totalPps, uint32_t activeFlows) {
        traffic_total_bps_ = totalBps;
        traffic_total_pps_ = totalPps;
        traffic_active_flows_ = activeFlows;
    }
    
    uint64_t trafficTotalBps() const { return traffic_total_bps_; }
    uint64_t trafficTotalPps() const { return traffic_total_pps_; }
    uint32_t trafficActiveFlows() const { return traffic_active_flows_; }

    // 用于比较是否同一网卡（以 ifname 为键）
    bool sameKey(const NetInfo& other) const { return ifname_ == other.ifname_; }

    // 等价比较（所有关键字段）
    bool equals(const NetInfo& other) const {
        return ifname_ == other.ifname_ && is_default_ == other.is_default_ && type_ == other.type_ && rtt_ms_ == other.rtt_ms_ && state_ == other.state_;
    }

private:
    std::string ifname_;
    bool is_default_ = false;
    NetType type_ = NetType::Unknown;
    int rtt_ms_ = -1;
    int prev_rtt_ms_ = -1;
    NetState state_ = NetState::Down;
    bool using_now_ = false;  // 是否当前被判定为"正在上网"的接口
    LinkQuality quality_ = LinkQuality::Unknown;
    int rssi_dbm_ = -1000; // Wi-Fi RSSI (dBm), 非 Wi-Fi 接口保持默认
    std::string ssid_;              // WiFi SSID（仅宿主 WiFi 桥接使用）
    double jitter_ms_ = 0.0;        // 延迟抖动（毫秒）
    int rtt_samples_ = 0;           // 用于计算当前 RTT/jitter 的 ping 样本数
    bool has_traffic_ = false;      // 采样窗口内是否有 WiFi 上下行流量
    uint64_t download_bps_ = 0;     // 下载速率（bytes/s）
    uint64_t upload_bps_ = 0;       // 上传速率（bytes/s）
    int link_rx_mbps_ = -1;         // WiFi 链路接收速率（Mbps）
    int link_tx_mbps_ = -1;         // WiFi 链路发送速率（Mbps）
    bool host_wifi_synthetic_ = false; // 是否为 WSL 下注入的宿主 WiFi 信息
    double tcp_loss_rate_ = -1.0; // TCP丢包率 (百分比)
    std::string tcp_loss_level_; // TCP丢包率等级 (good/degraded/poor/insufficient)
    
    // 流量统计
    uint64_t traffic_total_bps_ = 0; // 总带宽 (bytes per second)
    uint64_t traffic_total_pps_ = 0; // 总包速率 (packets per second)
    uint32_t traffic_active_flows_ = 0; // 活跃连接数
};

}  // namespace weaknet_dbus

