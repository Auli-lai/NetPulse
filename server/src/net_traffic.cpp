#include "net_traffic.h"

#include <arpa/inet.h>
#include <chrono>
#include <thread>
#include <algorithm>
#include <iostream>
#include <iomanip>
#include <cstring>
#include <net/if.h>
#include <unordered_map>
#include <sstream>
#include <errno.h>
#include <cmath>
#include <numeric>

#if defined(__has_include)
#  if __has_include(<linux/bpf.h>) && __has_include(<bpf/libbpf.h>) && __has_include(<bpf/bpf.h>)
#    define HAVE_LIBBPF 1
extern "C" {
#include <linux/bpf.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>
}
#  else
#    define HAVE_LIBBPF 0
#  endif
#else
#  define HAVE_LIBBPF 0
#endif

std::once_flag NetTrafficAnalyzer::s_onceFlag;
std::shared_ptr<NetTrafficAnalyzer> NetTrafficAnalyzer::s_instance;

std::shared_ptr<NetTrafficAnalyzer> NetTrafficAnalyzer::getInstance() {
    std::call_once(s_onceFlag, [](){ s_instance = std::shared_ptr<NetTrafficAnalyzer>(new NetTrafficAnalyzer()); });
    return s_instance;
}

void NetTrafficAnalyzer::setBpfObjectPath(const std::string& path) { bpfObjPath_ = path; }

static std::string ip_str(uint32_t ip) { struct in_addr a{ip}; return std::string(inet_ntoa(a)); }

bool NetTrafficAnalyzer::initForInterface(const std::string& ifaceName) {
#if !HAVE_LIBBPF
    (void)ifaceName;
    return false;
#else
    // libbpf 打印
    libbpf_set_strict_mode(LIBBPF_STRICT_ALL);
    libbpf_set_print([](enum libbpf_print_level level, const char *fmt, va_list args) -> int {
        (void)level; return vfprintf(stderr, fmt, args);
    });
    if (attached_) return true;
    bpf_object* obj = bpf_object__open(bpfObjPath_.c_str());
    if (!obj) return false;
    if (bpf_object__load(obj)) { bpf_object__close(obj); return false; }

    // map: current_sec 必须存在
    mapCurrFd_ = bpf_object__find_map_fd_by_name(obj, "current_sec");
    if (mapCurrFd_ < 0) { bpf_object__close(obj); return false; }

    // 可选：控制 map（按接口过滤，若内核态支持）
    mapCfgFd_ = bpf_object__find_map_fd_by_name(obj, "cfg_iface");
    if (mapCfgFd_ >= 0) {
        // 写入待绑定接口索引
        unsigned ifi = if_nametoindex(ifaceName.c_str());
        if (ifi > 0) {
            int zero = 0; unsigned val = ifi;
            bpf_map_update_elem(mapCfgFd_, &zero, &val, BPF_ANY);
        }
    }

    // attach 程序（允许 kprobe/fentry 自动识别）
    bpf_program* prog_tcp = bpf_object__find_program_by_name(obj, "tcp_transmit_entry");
    bpf_program* prog_udp = bpf_object__find_program_by_name(obj, "udp_send_entry");
    if (!prog_tcp || !prog_udp) { bpf_object__close(obj); return false; }
    bpf_link* l1 = bpf_program__attach(prog_tcp);
    long err_tcp = libbpf_get_error(l1);
    if (err_tcp) {
        fprintf(stderr, "[ebpf] attach kprobe/ip_queue_xmit failed: %ld errno=%d\n", err_tcp, errno);
        if (!l1) {/* noop */} else { bpf_link__destroy(l1); }
        l1 = nullptr;
    }
    bpf_link* l2 = bpf_program__attach(prog_udp);
    long err_udp = libbpf_get_error(l2);
    if (err_udp) {
        fprintf(stderr, "[ebpf] attach kprobe/udp_sendmsg failed: %ld errno=%d\n", err_udp, errno);
        if (!l2) {/* noop */} else { bpf_link__destroy(l2); }
        l2 = nullptr;
    }

    if (!l1 && !l2) { bpf_object__close(obj); return false; }
    bpfObj_ = obj; linkTcp_ = l1; linkUdp_ = l2; attached_ = true; boundIface_ = ifaceName;
    return true;
#endif
}

std::vector<FlowRate> NetTrafficAnalyzer::sampleTopFlows(int intervalSec, int topN) {
    std::vector<FlowRate> out;
    if (!attached_) return out;
#if !HAVE_LIBBPF
    return out;
#else
    // t0 快照
    struct conn_key { __u32 saddr, daddr; __u16 sport, dport; __u8 protocol; } key{}, next_key{};
    struct flow_data { __u64 bytes; __u64 packets; __u32 pid; } val{};
    auto keyStr = [](const conn_key& k){
        std::ostringstream oss; oss<<k.saddr<<":"<<k.sport<<"-"<<k.daddr<<":"<<k.dport<<"/"<<(int)k.protocol; return oss.str();
    };
    std::unordered_map<std::string, flow_data> snap;

    // ========== [perf] 计时开始：t0 遍历 BPF MAP 快照 ==========
    auto t0_start = std::chrono::steady_clock::now();
    int ret = bpf_map_get_next_key(mapCurrFd_, nullptr, &next_key);
    while (ret == 0) {
        if (bpf_map_lookup_elem(mapCurrFd_, &next_key, &val) == 0) {
            snap.emplace(keyStr(next_key), val);
        }
        key = next_key;
        ret = bpf_map_get_next_key(mapCurrFd_, &key, &next_key);
    }
    auto t0_end = std::chrono::steady_clock::now();
    auto t0_us = std::chrono::duration_cast<std::chrono::microseconds>(t0_end - t0_start).count();
    std::cout << "[perf] collect_metrics(t0 snapshot) cost: " << t0_us << " us, entries=" << snap.size() << std::endl;
    // ============================================================

    std::this_thread::sleep_for(std::chrono::seconds(intervalSec));

    // ========== [perf] 计时开始：t1 遍历 BPF MAP 增量读取 ==========
    auto t1_start = std::chrono::steady_clock::now();
    // t1 读取并计算 delta
    ret = bpf_map_get_next_key(mapCurrFd_, nullptr, &next_key);
    while (ret == 0) {
        if (bpf_map_lookup_elem(mapCurrFd_, &next_key, &val) == 0) {
            auto it = snap.find(keyStr(next_key));
            __u64 prev_bytes = 0, prev_pkts = 0; __u32 prev_pid = 0;
            if (it != snap.end()) { prev_bytes = it->second.bytes; prev_pkts = it->second.packets; prev_pid = it->second.pid; }
            __u64 dbytes = val.bytes - prev_bytes;
            __u64 dpkts  = val.packets - prev_pkts;
            if (dbytes || dpkts) {
                FlowRate fr;
                fr.src = ip_str(next_key.saddr);
                fr.dst = ip_str(next_key.daddr);
                fr.sport = ntohs(next_key.sport);
                fr.dport = ntohs(next_key.dport);
                fr.proto = (next_key.protocol == 6) ? "TCP" : (next_key.protocol == 17 ? "UDP" : std::to_string(next_key.protocol));
                fr.bps = dbytes / (uint64_t)intervalSec;
                fr.pps = dpkts  / (uint64_t)intervalSec;
                fr.pid = val.pid ? val.pid : prev_pid;
                out.push_back(fr);
            }
        }
        key = next_key;
        ret = bpf_map_get_next_key(mapCurrFd_, &key, &next_key);
    }
    auto t1_end = std::chrono::steady_clock::now();
    auto t1_us = std::chrono::duration_cast<std::chrono::microseconds>(t1_end - t1_start).count();
    std::cout << "[perf] collect_metrics(t1 delta) cost: " << t1_us << " us, flows=" << out.size() << std::endl;
    // =============================================================

    std::sort(out.begin(), out.end(), [](const FlowRate& a, const FlowRate& b){ return a.bps > b.bps; });
    if ((int)out.size() > topN) out.resize(topN);
    return out;
#endif
}

// 新增功能实现

std::string NetTrafficAnalyzer::generateFlowKey(const FlowRate& flow) {
    std::ostringstream oss;
    oss << flow.src << ":" << flow.sport << "-" << flow.dst << ":" << flow.dport << "/" << flow.proto;
    return oss.str();
}

bool NetTrafficAnalyzer::isBurstTraffic(const TrafficHistory& history, uint64_t currentBps) {
    if (history.bpsHistory.size() < 3) return false;
    
    // 计算历史平均值
    uint64_t avgBps = std::accumulate(history.bpsHistory.begin(), history.bpsHistory.end(), 0ULL) / history.bpsHistory.size();
    
    // 检查是否超过平均值的突发倍数阈值
    return currentBps > (avgBps * burstMultiplier_);
}

bool NetTrafficAnalyzer::isSuspiciousTraffic(uint64_t currentBps, uint32_t pid) {
    (void)pid;  // 预留：可扩展按特定 PID 判定异常流量
    // 检查是否超过可疑流量阈值
    if (currentBps < suspiciousThresholdBps_) return false;
    
    // 可以添加更多可疑行为检测逻辑
    // 例如：特定PID的异常流量模式
    return true;
}

double NetTrafficAnalyzer::calculateSeverity(uint64_t currentBps, uint64_t threshold, double multiplier) {
    if (currentBps <= threshold) return 0.0;
    
    double ratio = (double)currentBps / threshold;
    double severity = std::min(1.0, (ratio - 1.0) / (multiplier - 1.0));
    return severity;
}

std::vector<TrafficAnomaly> NetTrafficAnalyzer::detectAnomalies(int intervalSec, 
                                                               uint64_t burstThresholdBps,
                                                               uint64_t suspiciousThresholdBps,
                                                               double burstMultiplier) {
    std::vector<TrafficAnomaly> anomalies;
    
    if (!attached_) return anomalies;
    
    // 获取当前流量数据
    auto flows = sampleTopFlows(intervalSec, 1000); // 获取更多流进行分析
    
    std::lock_guard<std::mutex> lock(historyMutex_);
    auto now = std::chrono::system_clock::now();
    
    for (const auto& flow : flows) {
        std::string flowKey = generateFlowKey(flow);
        
        // 更新历史记录
        auto& history = trafficHistory_[flowKey];
        history.bpsHistory.push_back(flow.bps);
        history.ppsHistory.push_back(flow.pps);
        history.totalBytes += flow.bps * intervalSec;
        history.totalPackets += flow.pps * intervalSec;
        history.lastUpdate = now;
        
        // 限制历史记录大小
        if (history.bpsHistory.size() > MAX_HISTORY_SIZE) {
            history.bpsHistory.pop_front();
            history.ppsHistory.pop_front();
        }
        
        // 检测突发流量
        if (flow.bps > burstThresholdBps && isBurstTraffic(history, flow.bps)) {
            TrafficAnomaly anomaly;
            anomaly.flowKey = flowKey;
            anomaly.anomalyType = "burst";
            anomaly.currentBps = flow.bps;
            anomaly.thresholdBps = burstThresholdBps;
            anomaly.severity = calculateSeverity(flow.bps, burstThresholdBps, burstMultiplier);
            anomaly.timestamp = now;
            anomaly.description = "检测到突发流量: " + std::to_string(flow.bps / (1024*1024)) + " MB/s";
            anomalies.push_back(anomaly);
        }
        
        // 检测可疑流量
        if (isSuspiciousTraffic(flow.bps, flow.pid)) {
            TrafficAnomaly anomaly;
            anomaly.flowKey = flowKey;
            anomaly.anomalyType = "suspicious";
            anomaly.currentBps = flow.bps;
            anomaly.thresholdBps = suspiciousThresholdBps;
            anomaly.severity = calculateSeverity(flow.bps, suspiciousThresholdBps, 2.0);
            anomaly.timestamp = now;
            anomaly.description = "检测到可疑流量: " + std::to_string(flow.bps / (1024*1024)) + " MB/s, PID: " + std::to_string(flow.pid);
            anomalies.push_back(anomaly);
        }
        
        // 检测高流量
        if (flow.bps > suspiciousThresholdBps) {
            TrafficAnomaly anomaly;
            anomaly.flowKey = flowKey;
            anomaly.anomalyType = "high_volume";
            anomaly.currentBps = flow.bps;
            anomaly.thresholdBps = suspiciousThresholdBps;
            anomaly.severity = calculateSeverity(flow.bps, suspiciousThresholdBps, 1.5);
            anomaly.timestamp = now;
            anomaly.description = "检测到高流量: " + std::to_string(flow.bps / (1024*1024)) + " MB/s";
            anomalies.push_back(anomaly);
        }
    }
    
    return anomalies;
}

std::map<std::string, TrafficHistory> NetTrafficAnalyzer::getTrafficHistory() {
    std::lock_guard<std::mutex> lock(historyMutex_);
    return trafficHistory_;
}

void NetTrafficAnalyzer::setAnomalyDetectionParams(uint64_t burstThreshold, uint64_t suspiciousThreshold, double burstMultiplier) {
    std::lock_guard<std::mutex> lock(historyMutex_);
    burstThresholdBps_ = burstThreshold;
    suspiciousThresholdBps_ = suspiciousThreshold;
    burstMultiplier_ = burstMultiplier;
}

NetTrafficAnalyzer::RealTimeStats NetTrafficAnalyzer::getRealTimeStats() {
    RealTimeStats stats;
    
    if (!attached_) return stats;
    
    auto flows = sampleTopFlows(1, 1000); // 1秒采样
    
    stats.timestamp = std::chrono::system_clock::now();
    stats.activeFlows = flows.size();
    
    for (const auto& flow : flows) {
        stats.totalBps += flow.bps;
        stats.totalPps += flow.pps;
    }
    
    return stats;
}

void NetTrafficAnalyzer::clearHistory() {
    std::lock_guard<std::mutex> lock(historyMutex_);
    trafficHistory_.clear();
}

