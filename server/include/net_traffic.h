#pragma once

#include <string>
#include <vector>
#include <memory>
#include <mutex>
#include <cstdint>
#include <chrono>
#include <deque>
#include <map>

struct FlowRate {
    std::string src;
    std::string dst;
    int sport = 0;
    int dport = 0;
    std::string proto; // TCP/UDP
    uint64_t bps = 0;  // bytes per second
    uint64_t pps = 0;  // packets per second
    uint32_t pid = 0;
};

// 流量异常检测结果
struct TrafficAnomaly {
    std::string flowKey;        // 连接标识
    std::string anomalyType;    // 异常类型: "burst", "suspicious", "high_volume"
    uint64_t currentBps;        // 当前速率
    uint64_t thresholdBps;      // 阈值
    double severity;            // 严重程度 0.0-1.0
    std::chrono::system_clock::time_point timestamp;
    std::string description;    // 异常描述
};

// 流量历史记录
struct TrafficHistory {
    std::deque<uint64_t> bpsHistory;    // 历史速率记录
    std::deque<uint64_t> ppsHistory;    // 历史包速率记录
    std::chrono::system_clock::time_point lastUpdate;
    uint64_t totalBytes = 0;
    uint64_t totalPackets = 0;
};

class NetTrafficAnalyzer {
public:
    static std::shared_ptr<NetTrafficAnalyzer> getInstance();

    // 设置 eBPF 对象路径，默认 "build/flow_rate.bpf.o"
    void setBpfObjectPath(const std::string& path);

    // 初始化并附加到内核（按接口过滤），成功返回 true
    bool initForInterface(const std::string& ifaceName);

    // 采样 intervalSec 秒窗口，返回 TopN 流信息
    // 注意：内部会 sleep(intervalSec)，因为速率要靠两次快照求差
    std::vector<FlowRate> sampleTopFlows(int intervalSec, int topN);

    // 当前绑定的接口名（GetFlows 回包里要告诉调用方采的是哪块网卡）。
    // 只在 initForInterface 里赋值一次，之后只读，所以不需要加锁。
    const std::string& boundInterface() const { return boundIface_; }

    // 新增：异常流量检测功能
    std::vector<TrafficAnomaly> detectAnomalies(int intervalSec, 
                                               uint64_t burstThresholdBps = 10*1024*1024,  // 10MB/s
                                               uint64_t suspiciousThresholdBps = 50*1024*1024,  // 50MB/s
                                               double burstMultiplier = 3.0);  // 突发倍数阈值

    // 新增：获取流量历史统计
    std::map<std::string, TrafficHistory> getTrafficHistory();

    // 新增：设置异常检测参数
    void setAnomalyDetectionParams(uint64_t burstThreshold, uint64_t suspiciousThreshold, double burstMultiplier);

    // 新增：获取实时流量统计
    struct RealTimeStats {
        uint64_t totalBps = 0;
        uint64_t totalPps = 0;
        size_t activeFlows = 0;
        std::chrono::system_clock::time_point timestamp;
    };
    RealTimeStats getRealTimeStats();

    // 新增：清理历史数据
    void clearHistory();

private:
    NetTrafficAnalyzer() = default;
    static std::once_flag s_onceFlag;
    static std::shared_ptr<NetTrafficAnalyzer> s_instance;

    std::string bpfObjPath_ = "build/flow_rate.bpf.o";
    std::string boundIface_;

    // 句柄
    void* bpfObj_ = nullptr; // bpf_object*
    void* linkTcp_ = nullptr; // bpf_link*
    void* linkUdp_ = nullptr; // bpf_link*
    int mapCurrFd_ = -1;
    int mapCfgFd_  = -1;
    bool attached_ = false;

    // 新增：异常检测相关
    mutable std::mutex historyMutex_;
    std::map<std::string, TrafficHistory> trafficHistory_;
    uint64_t burstThresholdBps_ = 10*1024*1024;      // 10MB/s
    uint64_t suspiciousThresholdBps_ = 50*1024*1024;  // 50MB/s
    double burstMultiplier_ = 3.0;                   // 突发倍数阈值
    static constexpr size_t MAX_HISTORY_SIZE = 60;   // 保留60个历史记录

    // 内部方法
    std::string generateFlowKey(const FlowRate& flow);
    bool isBurstTraffic(const TrafficHistory& history, uint64_t currentBps);
    bool isSuspiciousTraffic(uint64_t currentBps, uint32_t pid);
    double calculateSeverity(uint64_t currentBps, uint64_t threshold, double multiplier);
};
