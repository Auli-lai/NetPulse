// traffic_analyzer.cpp
// 流量分析线程实现

#include "traffic_analyzer.hpp"
#include "logger.hpp"
#include <chrono>
#include <thread>

namespace weaknet_dbus {

TrafficAnalyzer::TrafficAnalyzer() 
    : running_(false), interval_seconds_(10) {
    analyzer_ = NetTrafficAnalyzer::getInstance();
}

TrafficAnalyzer::~TrafficAnalyzer() {
    stop();
}

void TrafficAnalyzer::start(const std::string& interface, int interval_seconds) {
    if (running_.load()) {
        LOG_INFO(LogModule::WEAK_MGR, "Traffic analyzer already running");
        return;
    }
    
    interface_ = interface;
    interval_seconds_ = interval_seconds;
    
    // 设置eBPF对象路径
    analyzer_->setBpfObjectPath("../build/flow_rate.bpf.o");
    
    // 设置异常检测参数
    analyzer_->setAnomalyDetectionParams(
        5 * 1024 * 1024,    // 突发阈值: 5MB/s
        20 * 1024 * 1024,   // 可疑阈值: 20MB/s
        2.5                 // 突发倍数: 2.5倍
    );
    
    // 初始化网络接口
    if (!analyzer_->initForInterface(interface)) {
        LOG_ERROR(LogModule::WEAK_MGR, "Failed to initialize traffic analyzer for interface: " << interface);
        LOG_INFO(LogModule::WEAK_MGR, "Traffic analyzer will run in degraded mode (no eBPF monitoring)");
        // 不返回，继续运行但跳过eBPF功能
    }
    
    running_.store(true);
    thread_ = std::make_unique<std::thread>(&TrafficAnalyzer::analyzeLoop, this);
    
    LOG_INFO(LogModule::WEAK_MGR, "Traffic analyzer started for interface: " << interface << " (interval=" << interval_seconds << "s)");
}

void TrafficAnalyzer::stop() {
    if (!running_.load()) {
        return;
    }
    
    running_.store(false);
    
    if (thread_ && thread_->joinable()) {
        thread_->join();
    }
    
    // 清理历史数据
    analyzer_->clearHistory();
    
    LOG_INFO(LogModule::WEAK_MGR, "Traffic analyzer stopped");
}

void TrafficAnalyzer::analyzeLoop() {
    LOG_INFO(LogModule::WEAK_MGR, "Traffic analysis loop started");
    
    while (running_.load()) {
        try {
            // 获取实时统计（如果eBPF可用）
            NetTrafficAnalyzer::RealTimeStats stats;
            bool hasStats = false;
            
            try {
                stats = analyzer_->getRealTimeStats();
                hasStats = true;
                
                // 更新缓存
                {
                    std::lock_guard<std::mutex> lock(stats_mutex_);
                    cached_stats_ = stats;
                }
            } catch (const std::exception& e) {
                LOG_INFO(LogModule::WEAK_MGR, "Traffic stats unavailable (eBPF not working): " << e.what());
            }
            
            // 检测异常流量（如果eBPF可用）
            if (hasStats) {
                try {
                    auto anomalies = analyzer_->detectAnomalies(5);
                    if (!anomalies.empty()) {
                        LOG_INFO(LogModule::WEAK_MGR, "Detected " << anomalies.size() << " traffic anomalies");
                        for (const auto& anomaly : anomalies) {
                            LOG_INFO(LogModule::WEAK_MGR, "Anomaly: " << anomaly.anomalyType 
                                << " on " << anomaly.flowKey 
                                << " (severity: " << (anomaly.severity * 100) << "%)");
                        }
                    }
                } catch (const std::exception& e) {
                    LOG_INFO(LogModule::WEAK_MGR, "Anomaly detection unavailable: " << e.what());
                }
            }
            
            // 记录详细流量统计
            if (hasStats) {
                LOG_INFO(LogModule::WEAK_MGR, "TRAFFIC_MONITOR: Total=" << (stats.totalBps / (1024*1024)) 
                    << "MB/s, Flows=" << stats.activeFlows 
                    << ", PPS=" << stats.totalPps
                    << ", Interface=" << interface_);
                    
                // 获取Top流量连接并记录
                try {
                    auto topFlows = analyzer_->sampleTopFlows(5, 5);
                    if (!topFlows.empty()) {
                        LOG_INFO(LogModule::WEAK_MGR, "TOP_FLOWS: ");
                        for (size_t i = 0; i < std::min(topFlows.size(), size_t(3)); ++i) {
                            const auto& flow = topFlows[i];
                            LOG_INFO(LogModule::WEAK_MGR, "  " << (i+1) << ". " << flow.proto 
                                << " " << flow.src << ":" << flow.sport 
                                << " -> " << flow.dst << ":" << flow.dport 
                                << " | " << (flow.bps / 1024) << "KB/s, " << flow.pps << "pps");
                        }
                    }
                } catch (const std::exception& e) {
                    LOG_INFO(LogModule::WEAK_MGR, "Top flows unavailable: " << e.what());
                }
            } else {
                LOG_INFO(LogModule::WEAK_MGR, "TRAFFIC_MONITOR: Running in degraded mode (no eBPF data available)");
            }
            
        } catch (const std::exception& e) {
            LOG_ERROR(LogModule::WEAK_MGR, "Traffic analysis error: " << e.what());
        }
        
        // 等待下一个分析周期
        for (int i = 0; i < interval_seconds_ && running_.load(); ++i) {
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }
    }
    
    LOG_INFO(LogModule::WEAK_MGR, "Traffic analysis loop stopped");
}

NetTrafficAnalyzer::RealTimeStats TrafficAnalyzer::getCurrentStats() const {
    std::lock_guard<std::mutex> lock(stats_mutex_);
    return cached_stats_;
}

std::vector<FlowRate> TrafficAnalyzer::getTopFlows(int sample_seconds, int top_count) const {
    return analyzer_->sampleTopFlows(sample_seconds, top_count);
}

std::vector<TrafficAnomaly> TrafficAnalyzer::detectAnomalies(int detection_seconds) const {
    return analyzer_->detectAnomalies(detection_seconds);
}

std::map<std::string, TrafficHistory> TrafficAnalyzer::getTrafficHistory() const {
    return analyzer_->getTrafficHistory();
}

} // namespace weaknet_dbus
