#pragma once

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>

struct TcpStats {
    uint64_t inSegs = 0;      // Tcp: InSegs
    uint64_t outSegs = 0;     // Tcp: OutSegs
    uint64_t retransSegs = 0; // Tcp: RetransSegs
    bool valid = false;
};

struct TcpLossResult {
    double ratePercent = 0.0;   // (deltaRetrans / deltaOut) * 100
    uint64_t sentDelta = 0;     // delta OutSegs
    uint64_t retransDelta = 0;  // delta RetransSegs
    std::string level;          // good/degraded/poor/insufficient
};

class TcpLossMonitor {
public:
    static std::shared_ptr<TcpLossMonitor> getInstance();

    // 读取系统 TCP 计数（来自 /proc/net/snmp）
    bool sample(TcpStats& outStats);
    // 针对指定接口名统计（优先用 netlink 聚合该接口的 tcp_info；必要时回落）
    bool sampleForInterface(const std::string& ifaceName, TcpStats& outStats);

    // 基于两次采样计算失败率；minSent 为阈值（默认10）
    TcpLossResult compute(const TcpStats& prev,
                          const TcpStats& curr,
                          uint64_t minSent = 10,
                          double degradedThresholdPct = 1.0,
                          double poorThresholdPct = 5.0);

private:
    TcpLossMonitor() = default;
    static std::once_flag s_onceFlag;
    static std::shared_ptr<TcpLossMonitor> s_instance;
};
