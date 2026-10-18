#include "traffic_anomaly_detector.h"
#include <iostream>
#include <numeric>
#include <algorithm>
#include <cmath>
#include <sstream>

TrafficAnomalyDetector::TrafficAnomalyDetector() 
    : burstThreshold_(2.5), volumeThreshold_(3.0), timeThreshold_(2.0) {
}

void TrafficAnomalyDetector::setDetectionParams(double burstThreshold, double volumeThreshold, double timeThreshold) {
    std::lock_guard<std::mutex> lock(patternsMutex_);
    burstThreshold_ = burstThreshold;
    volumeThreshold_ = volumeThreshold;
    timeThreshold_ = timeThreshold;
}

void TrafficAnomalyDetector::updateTrafficPattern(const FlowRate& flow) {
    std::string flowKey = flow.src + ":" + std::to_string(flow.sport) + "-" + 
                         flow.dst + ":" + std::to_string(flow.dport) + "/" + flow.proto;
    
    auto& pattern = trafficPatterns_[flowKey];
    pattern.flowKey = flowKey;
    
    auto now = std::chrono::system_clock::now();
    
    if (pattern.bpsHistory.empty()) {
        pattern.firstSeen = now;
    }
    pattern.lastSeen = now;
    
    pattern.bpsHistory.push_back(flow.bps);
    pattern.ppsHistory.push_back(flow.pps);
    pattern.totalBytes += flow.bps;
    pattern.totalPackets += flow.pps;
    
    // 限制历史记录大小
    const size_t MAX_HISTORY = 100;
    if (pattern.bpsHistory.size() > MAX_HISTORY) {
        pattern.bpsHistory.erase(pattern.bpsHistory.begin());
        pattern.ppsHistory.erase(pattern.ppsHistory.begin());
    }
    
    // 计算统计指标
    if (pattern.bpsHistory.size() > 1) {
        pattern.avgBps = std::accumulate(pattern.bpsHistory.begin(), pattern.bpsHistory.end(), 0.0) / pattern.bpsHistory.size();
        pattern.stdDevBps = calculateStandardDeviation(pattern.bpsHistory);
    }
}

double TrafficAnomalyDetector::calculateStandardDeviation(const std::vector<uint64_t>& values) {
    if (values.size() < 2) return 0.0;
    
    double mean = std::accumulate(values.begin(), values.end(), 0.0) / values.size();
    double variance = 0.0;
    
    for (uint64_t value : values) {
        variance += std::pow(value - mean, 2);
    }
    variance /= values.size();
    
    return std::sqrt(variance);
}

double TrafficAnomalyDetector::calculateZScore(uint64_t value, double mean, double stdDev) {
    if (stdDev == 0.0) return 0.0;
    return (value - mean) / stdDev;
}

double TrafficAnomalyDetector::calculatePercentile(const std::vector<uint64_t>& values, double percentile) {
    if (values.empty()) return 0.0;
    
    std::vector<uint64_t> sortedValues = values;
    std::sort(sortedValues.begin(), sortedValues.end());
    
    size_t index = static_cast<size_t>((percentile / 100.0) * (sortedValues.size() - 1));
    return sortedValues[index];
}

bool TrafficAnomalyDetector::isOutlier(uint64_t value, const std::vector<uint64_t>& values) {
    if (values.size() < 3) return false;
    
    double mean = std::accumulate(values.begin(), values.end(), 0.0) / values.size();
    double stdDev = calculateStandardDeviation(values);
    double zScore = calculateZScore(value, mean, stdDev);
    
    // 使用3-sigma规则检测异常值
    return std::abs(zScore) > 3.0;
}

std::vector<AdvancedAnomaly> TrafficAnomalyDetector::analyzeTrafficPatterns(const std::vector<FlowRate>& flows) {
    std::vector<AdvancedAnomaly> anomalies;
    
    std::lock_guard<std::mutex> lock(patternsMutex_);
    
    // 更新所有流量模式
    for (const auto& flow : flows) {
        updateTrafficPattern(flow);
    }
    
    // 分析每个模式
    for (auto& [flowKey, pattern] : trafficPatterns_) {
        // 检测数据泄露
        if (isDataExfiltrationPattern(pattern)) {
            AdvancedAnomaly anomaly;
            anomaly.flowKey = flowKey;
            anomaly.anomalyType = "data_exfiltration";
            anomaly.confidence = calculateAnomalyConfidence(pattern, "data_exfiltration");
            anomaly.severity = std::min(1.0, pattern.avgBps / (10 * 1024 * 1024)); // 相对于10MB/s的严重程度
            anomaly.description = "检测到可能的数据泄露行为";
            anomaly.timestamp = std::chrono::system_clock::now();
            anomaly.metrics["avg_bps"] = pattern.avgBps;
            anomaly.metrics["total_bytes"] = pattern.totalBytes;
            anomaly.metrics["duration_minutes"] = std::chrono::duration_cast<std::chrono::minutes>(
                pattern.lastSeen - pattern.firstSeen).count();
            anomalies.push_back(anomaly);
        }
        
        // 检测可疑连接
        if (isSuspiciousConnectionPattern(pattern)) {
            AdvancedAnomaly anomaly;
            anomaly.flowKey = flowKey;
            anomaly.anomalyType = "suspicious_connection";
            anomaly.confidence = calculateAnomalyConfidence(pattern, "suspicious_connection");
            anomaly.severity = std::min(1.0, pattern.stdDevBps / pattern.avgBps);
            anomaly.description = "检测到可疑连接模式";
            anomaly.timestamp = std::chrono::system_clock::now();
            anomaly.metrics["std_dev"] = pattern.stdDevBps;
            anomaly.metrics["coefficient_variation"] = pattern.stdDevBps / pattern.avgBps;
            anomalies.push_back(anomaly);
        }
        
        // 检测时间异常
        if (isTemporalAnomaly(pattern)) {
            AdvancedAnomaly anomaly;
            anomaly.flowKey = flowKey;
            anomaly.anomalyType = "temporal_anomaly";
            anomaly.confidence = calculateAnomalyConfidence(pattern, "temporal_anomaly");
            anomaly.severity = 0.7; // 时间异常通常中等严重程度
            anomaly.description = "检测到时间模式异常";
            anomaly.timestamp = std::chrono::system_clock::now();
            auto duration = std::chrono::duration_cast<std::chrono::minutes>(pattern.lastSeen - pattern.firstSeen);
            anomaly.metrics["duration_minutes"] = duration.count();
            anomalies.push_back(anomaly);
        }
    }
    
    return anomalies;
}

bool TrafficAnomalyDetector::isDataExfiltrationPattern(const TrafficPattern& pattern) {
    if (pattern.bpsHistory.size() < 5) return false;
    
    // 检查持续高流量
    uint64_t highBpsCount = 0;
    uint64_t threshold = 5 * 1024 * 1024; // 5MB/s
    
    for (uint64_t bps : pattern.bpsHistory) {
        if (bps > threshold) {
            highBpsCount++;
        }
    }
    
    // 如果超过50%的时间都是高流量，可能是数据泄露
    double highBpsRatio = static_cast<double>(highBpsCount) / pattern.bpsHistory.size();
    
    // 检查流量稳定性（数据泄露通常比较稳定）
    double stability = 1.0 - (pattern.stdDevBps / pattern.avgBps);
    
    return highBpsRatio > 0.5 && stability > 0.3 && pattern.avgBps > threshold;
}

bool TrafficAnomalyDetector::isSuspiciousConnectionPattern(const TrafficPattern& pattern) {
    if (pattern.bpsHistory.size() < 3) return false;
    
    // 检查流量波动性
    double coefficientOfVariation = pattern.stdDevBps / pattern.avgBps;
    
    // 检查是否有异常峰值
    bool hasOutliers = false;
    for (uint64_t bps : pattern.bpsHistory) {
        if (isOutlier(bps, pattern.bpsHistory)) {
            hasOutliers = true;
            break;
        }
    }
    
    return coefficientOfVariation > 1.0 || hasOutliers;
}

bool TrafficAnomalyDetector::isTemporalAnomaly(const TrafficPattern& pattern) {
    auto now = std::chrono::system_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::minutes>(now - pattern.lastSeen);
    
    // 检查是否在非工作时间有大量流量
    auto time_t = std::chrono::system_clock::to_time_t(now);
    auto tm = *std::localtime(&time_t);
    int hour = tm.tm_hour;
    
    bool isOffHours = (hour < 8 || hour > 18); // 工作时间外
    bool hasHighVolume = pattern.avgBps > (2 * 1024 * 1024); // 2MB/s
    
    return isOffHours && hasHighVolume;
}

double TrafficAnomalyDetector::calculateAnomalyConfidence(const TrafficPattern& pattern, const std::string& anomalyType) {
    double confidence = 0.0;
    
    if (anomalyType == "data_exfiltration") {
        // 基于流量大小和稳定性计算置信度
        double volumeScore = std::min(1.0, pattern.avgBps / (20 * 1024 * 1024)); // 20MB/s为满分
        double stabilityScore = 1.0 - std::min(1.0, pattern.stdDevBps / pattern.avgBps);
        confidence = (volumeScore + stabilityScore) / 2.0;
    }
    else if (anomalyType == "suspicious_connection") {
        // 基于流量波动性计算置信度
        double variationScore = std::min(1.0, pattern.stdDevBps / pattern.avgBps);
        confidence = variationScore;
    }
    else if (anomalyType == "temporal_anomaly") {
        // 基于时间模式计算置信度
        auto now = std::chrono::system_clock::now();
        auto time_t = std::chrono::system_clock::to_time_t(now);
        auto tm = *std::localtime(&time_t);
        int hour = tm.tm_hour;
        
        double timeScore = 0.0;
        if (hour < 6 || hour > 22) timeScore = 1.0; // 深夜
        else if (hour < 8 || hour > 20) timeScore = 0.7; // 早晚
        else timeScore = 0.3; // 工作时间
        
        confidence = timeScore;
    }
    
    return std::min(1.0, std::max(0.0, confidence));
}

std::vector<AdvancedAnomaly> TrafficAnomalyDetector::detectDataExfiltration(const std::vector<FlowRate>& flows) {
    std::vector<AdvancedAnomaly> anomalies;
    
    for (const auto& flow : flows) {
        // 检测大文件传输
        if (flow.bps > 10 * 1024 * 1024) { // 10MB/s
            AdvancedAnomaly anomaly;
            anomaly.flowKey = flow.src + ":" + std::to_string(flow.sport) + "-" + 
                             flow.dst + ":" + std::to_string(flow.dport) + "/" + flow.proto;
            anomaly.anomalyType = "data_exfiltration";
            anomaly.confidence = std::min(1.0, flow.bps / (50.0 * 1024 * 1024));
            anomaly.severity = std::min(1.0, flow.bps / (100.0 * 1024 * 1024));
            anomaly.description = "检测到高流量数据传输，可能存在数据泄露";
            anomaly.timestamp = std::chrono::system_clock::now();
            anomaly.metrics["current_bps"] = flow.bps;
            anomaly.metrics["pid"] = flow.pid;
            anomalies.push_back(anomaly);
        }
    }
    
    return anomalies;
}

std::vector<AdvancedAnomaly> TrafficAnomalyDetector::detectSuspiciousConnections(const std::vector<FlowRate>& flows) {
    std::vector<AdvancedAnomaly> anomalies;
    
    // 统计每个进程的连接数
    std::map<uint32_t, int> pidConnectionCount;
    for (const auto& flow : flows) {
        if (flow.pid > 0) {
            pidConnectionCount[flow.pid]++;
        }
    }
    
    // 检测异常多连接的进程
    for (const auto& [pid, count] : pidConnectionCount) {
        if (count > 50) { // 超过50个连接
            AdvancedAnomaly anomaly;
            anomaly.flowKey = "PID:" + std::to_string(pid);
            anomaly.anomalyType = "suspicious_connection";
            anomaly.confidence = std::min(1.0, count / 200.0);
            anomaly.severity = std::min(1.0, count / 100.0);
            anomaly.description = "进程 " + std::to_string(pid) + " 有异常多的连接数: " + std::to_string(count);
            anomaly.timestamp = std::chrono::system_clock::now();
            anomaly.metrics["connection_count"] = count;
            anomaly.metrics["pid"] = pid;
            anomalies.push_back(anomaly);
        }
    }
    
    return anomalies;
}

std::vector<AdvancedAnomaly> TrafficAnomalyDetector::detectTemporalAnomalies(const std::vector<FlowRate>& flows) {
    std::vector<AdvancedAnomaly> anomalies;
    
    auto now = std::chrono::system_clock::now();
    auto time_t = std::chrono::system_clock::to_time_t(now);
    auto tm = *std::localtime(&time_t);
    
    // 检查是否在非工作时间
    bool isOffHours = (tm.tm_hour < 8 || tm.tm_hour > 18);
    
    if (isOffHours) {
        uint64_t totalBps = 0;
        for (const auto& flow : flows) {
            totalBps += flow.bps;
        }
        
        if (totalBps > 5 * 1024 * 1024) { // 5MB/s
            AdvancedAnomaly anomaly;
            anomaly.flowKey = "temporal_anomaly";
            anomaly.anomalyType = "temporal_anomaly";
            anomaly.confidence = 0.8;
            anomaly.severity = std::min(1.0, totalBps / (20.0 * 1024 * 1024));
            anomaly.description = "在非工作时间检测到高流量活动";
            anomaly.timestamp = now;
            anomaly.metrics["total_bps"] = totalBps;
            anomaly.metrics["hour"] = tm.tm_hour;
            anomalies.push_back(anomaly);
        }
    }
    
    return anomalies;
}

std::map<std::string, TrafficPattern> TrafficAnomalyDetector::getTrafficPatterns() {
    std::lock_guard<std::mutex> lock(patternsMutex_);
    return trafficPatterns_;
}

void TrafficAnomalyDetector::clearHistory() {
    std::lock_guard<std::mutex> lock(patternsMutex_);
    trafficPatterns_.clear();
}
