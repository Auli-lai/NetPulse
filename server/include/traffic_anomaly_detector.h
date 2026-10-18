#pragma once

#include "net_traffic.h"
#include <vector>
#include <map>
#include <chrono>
#include <mutex>
#include <algorithm>
#include <cmath>

// 流量模式分析
struct TrafficPattern {
    std::string flowKey;
    std::vector<uint64_t> bpsHistory;
    std::vector<uint64_t> ppsHistory;
    std::chrono::system_clock::time_point firstSeen;
    std::chrono::system_clock::time_point lastSeen;
    uint64_t totalBytes = 0;
    uint64_t totalPackets = 0;
    double avgBps = 0.0;
    double stdDevBps = 0.0;
    bool isSuspicious = false;
};

// 高级异常检测结果
struct AdvancedAnomaly {
    std::string flowKey;
    std::string anomalyType;
    double confidence;  // 置信度 0.0-1.0
    double severity;    // 严重程度 0.0-1.0
    std::string description;
    std::map<std::string, double> metrics;  // 各种指标
    std::chrono::system_clock::time_point timestamp;
};

class TrafficAnomalyDetector {
public:
    TrafficAnomalyDetector();
    ~TrafficAnomalyDetector() = default;

    // 分析流量模式并检测异常
    std::vector<AdvancedAnomaly> analyzeTrafficPatterns(const std::vector<FlowRate>& flows);

    // 检测偷跑流量行为
    std::vector<AdvancedAnomaly> detectDataExfiltration(const std::vector<FlowRate>& flows);

    // 检测异常连接模式
    std::vector<AdvancedAnomaly> detectSuspiciousConnections(const std::vector<FlowRate>& flows);

    // 检测时间模式异常
    std::vector<AdvancedAnomaly> detectTemporalAnomalies(const std::vector<FlowRate>& flows);

    // 获取流量模式统计
    std::map<std::string, TrafficPattern> getTrafficPatterns();

    // 设置检测参数
    void setDetectionParams(double burstThreshold, double volumeThreshold, double timeThreshold);

    // 清理历史数据
    void clearHistory();

private:
    mutable std::mutex patternsMutex_;
    std::map<std::string, TrafficPattern> trafficPatterns_;
    
    // 检测参数
    double burstThreshold_;      // 突发阈值倍数
    double volumeThreshold_;    // 流量阈值倍数
    double timeThreshold_;      // 时间异常阈值
    
    // 内部方法
    void updateTrafficPattern(const FlowRate& flow);
    double calculateStandardDeviation(const std::vector<uint64_t>& values);
    bool isDataExfiltrationPattern(const TrafficPattern& pattern);
    bool isSuspiciousConnectionPattern(const TrafficPattern& pattern);
    bool isTemporalAnomaly(const TrafficPattern& pattern);
    double calculateAnomalyConfidence(const TrafficPattern& pattern, const std::string& anomalyType);
    
    // 统计方法
    double calculateZScore(uint64_t value, double mean, double stdDev);
    double calculatePercentile(const std::vector<uint64_t>& values, double percentile);
    bool isOutlier(uint64_t value, const std::vector<uint64_t>& values);
};
