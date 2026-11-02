#pragma once

#include <thread>
#include <atomic>
#include <string>
#include <memory>
#include "net_traffic.h"

namespace weaknet_dbus {

// 流量分析线程管理器
class TrafficAnalyzer {
public:
    TrafficAnalyzer();
    ~TrafficAnalyzer();

    // 启动流量分析线程
    void start(const std::string& interface, int interval_seconds = 10);
    
    // 停止流量分析线程
    void stop();
    
    // 检查是否正在运行
    bool isRunning() const { return running_.load(); }
    
    // 获取当前流量统计
    NetTrafficAnalyzer::RealTimeStats getCurrentStats() const;
    
    // 获取Top流量连接
    std::vector<FlowRate> getTopFlows(int sample_seconds = 5, int top_count = 10) const;
    
    // 检测流量异常
    std::vector<TrafficAnomaly> detectAnomalies(int detection_seconds = 5) const;
    
    // 获取流量历史
    std::map<std::string, TrafficHistory> getTrafficHistory() const;

private:
    void analyzeLoop();
    
    std::unique_ptr<std::thread> thread_;
    std::atomic<bool> running_;
    std::string interface_;
    int interval_seconds_;
    
    // 流量分析器实例
    std::shared_ptr<NetTrafficAnalyzer> analyzer_;
    
    // 线程安全的状态缓存
    mutable std::mutex stats_mutex_;
    NetTrafficAnalyzer::RealTimeStats cached_stats_;
};

} // namespace weaknet_dbus
