// weak_netmgr.hpp
// 定义 WeakNetMgr：管理 NetInfo 列表与质量指标更新

#pragma once

#include <vector>
#include <string>
#include <memory>
#include <mutex>

#include "net_info.hpp"
#include "traffic_analyzer.hpp"

namespace weaknet_dbus {

class WeakNetMgr {
private:
    std::shared_ptr<TrafficAnalyzer> traffic_analyzer_;
    mutable std::mutex iface_mutex_;  // 保护接口列表的互斥锁
    std::vector<NetInfo> current_interfaces_;  // 当前接口列表

public:
    WeakNetMgr() : iface_mutex_(), current_interfaces_() {}

    // 从底层查询或外部模块同步当前具备上网能力的接口，返回 NetInfo 列表
    // 这里默认通过 NetInterfaceManager 获取接口名，再填充基本字段，后续可扩展 RTT 等
    std::vector<NetInfo> collectCurrentInterfaces();

    // 根据接口名获取 NetInfo（如果存在）
    bool findByName(const std::vector<NetInfo>& list, const std::string& ifname, NetInfo* out) const;

    // 将 NetInfo 列表转为接口名数组（用于 D-Bus 返回）
    static std::vector<std::string> namesOf(const std::vector<NetInfo>& list);

    // 使用 using_iface 更新当前上网网卡标志（usingNow）
    // - 传入列表按引用更新 usingNow 标志
    // - printLog: 若为 true 则在变化时打印日志
    // - 可选输出当前网卡名与标志位
    // 返回值：是否发生变化
    bool updateCurrentUsing(std::vector<NetInfo>& list,
                            bool printLog,
                            std::string* outIfName = nullptr,
                            uint32_t* outFlags = nullptr);

    // 更新 RTT 与链路质量；返回是否有任何变化
    // host: 目标域名/IP；timeoutMs: 单次超时
    bool updateRttAndState(std::vector<NetInfo>& list, const std::string& host, int timeoutMs = 800);

    // 更新 Wi-Fi RSSI：仅对类型为 WiFi 的接口调用，若是当前上网网卡则打印 using 标记
    // ctrlDir: wpa_supplicant 控制目录（可留空自动探测）
    bool updateWifiRssi(std::vector<NetInfo>& list, const std::string& ctrlDir = "");

    // 更新指定接口的丢包率：根据接口名查找并更新TCP丢包率和等级
    // 返回是否有变化
    bool updateTcpLossRate(std::vector<NetInfo>& list, 
                          const std::string& iface_name, 
                          double loss_rate, 
                          const std::string& loss_level);

    // 流量分析相关函数
    // 启动流量分析器
    void startTrafficAnalysis(const std::string& interface, int interval_seconds = 10);
    
    // 停止流量分析器
    void stopTrafficAnalysis();
    
    // 更新当前上网网卡的流量分析数据
    bool updateTrafficAnalysis(std::vector<NetInfo>& list);
    
    // 获取流量分析器实例
    std::shared_ptr<TrafficAnalyzer> getTrafficAnalyzer() const;

    // 线程安全的接口列表操作方法
    // 获取当前接口列表的副本（线程安全）
    std::vector<NetInfo> getCurrentInterfaces() const;
    
    // 更新接口列表（线程安全）
    void updateInterfaces(const std::vector<NetInfo>& new_interfaces);
    
    // 线程安全的RTT更新
    bool updateRttAndStateSafe(const std::string& host, int timeoutMs = 800);
    
    // 线程安全的RSSI更新
    bool updateWifiRssiSafe(const std::string& ctrlDir = "");
    
    // 线程安全的TCP丢包率更新
    bool updateTcpLossRateSafe(const std::string& iface_name, double loss_rate, const std::string& loss_level);
    
    // 线程安全的流量分析更新
    bool updateTrafficAnalysisSafe();
    
    // 线程安全的当前使用接口更新
    bool updateCurrentUsingSafe();
};

}  // namespace weaknet_dbus


