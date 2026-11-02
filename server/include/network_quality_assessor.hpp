// network_quality_assessor.hpp
// 网络质量评估器：基于多个指标评估网络质量

#ifndef NETWORK_QUALITY_ASSESSOR_HPP
#define NETWORK_QUALITY_ASSESSOR_HPP

#include <string>
#include <map>
#include <vector>
#include "net_info.hpp"

namespace weaknet_dbus {

// 网络质量等级
enum class NetworkQualityLevel {
    EXCELLENT = 4,  // 优秀
    GOOD = 3,       // 良好
    FAIR = 2,       // 一般
    POOR = 1,       // 差
    UNKNOWN = 0     // 未知
};

// 网络质量评估结果
struct NetworkQualityResult {
    NetworkQualityLevel level;
    std::string levelName;
    std::string details;  // JSON格式的详细指标
    double score;         // 0-100的质量分数
    std::vector<std::string> issues;  // 发现的问题列表
};

class NetworkQualityAssessor {
private:
    // 质量评估阈值配置
    struct QualityThresholds {
        // RTT阈值（毫秒）
        int rtt_excellent = 50;
        int rtt_good = 100;
        int rtt_fair = 200;
        
        // TCP丢包率阈值（百分比）
        double tcp_loss_excellent = 0.1;
        double tcp_loss_good = 0.5;
        double tcp_loss_fair = 2.0;
        
        // RSSI阈值（dBm）
        int rssi_excellent = -50;
        int rssi_good = -60;
        int rssi_fair = -70;
        
        // 流量异常阈值
        double traffic_anomaly_threshold = 0.8;  // 异常流量比例阈值
        uint32_t min_flows_for_analysis = 5;     // 最小流数量用于分析
    };

    QualityThresholds thresholds_;
    NetworkQualityResult lastResult_;
    int32_t qualityChangeCounter_;

public:
    NetworkQualityAssessor();
    
    // 评估网络质量
    NetworkQualityResult assessQuality(const std::vector<NetInfo>& interfaces);
    
    // 评估单个接口质量
    NetworkQualityResult assessInterfaceQuality(const NetInfo& interface);
    
    // 获取质量等级名称
    static std::string getQualityLevelName(NetworkQualityLevel level);
    
    // 生成详细质量信息（JSON格式）
    std::string generateQualityDetails(const NetInfo& interface, double score, const std::vector<std::string>& issues);
    
    // 检查是否有质量变化
    bool hasQualityChanged(const NetworkQualityResult& current);
    
    // 获取质量变化计数器
    int32_t getQualityChangeCounter() const { return qualityChangeCounter_; }
    
    // 更新阈值配置
    void updateThresholds(const QualityThresholds& newThresholds);
    
private:
    // 计算RTT质量分数
    double calculateRttScore(int rttMs);
    
    // 计算TCP丢包质量分数
    double calculateTcpLossScore(double lossRate);
    
    // 计算RSSI质量分数
    double calculateRssiScore(int rssiDbm);
    
    // 计算流量质量分数
    double calculateTrafficScore(const NetInfo& interface);
    
    // 检测网络问题
    std::vector<std::string> detectNetworkIssues(const NetInfo& interface, double score);
    
    // 生成JSON格式的详细指标
    std::string generateMetricsJson(const NetInfo& interface, double score, const std::vector<std::string>& issues);
};

} // namespace weaknet_dbus

#endif // NETWORK_QUALITY_ASSESSOR_HPP
