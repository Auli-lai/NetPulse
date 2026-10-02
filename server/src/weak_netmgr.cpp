// weak_netmgr.cpp
// 实现 WeakNetMgr：生成 NetInfo 列表

#include "weak_netmgr.hpp"
#include "net_iface.h"
#include "using_iface.h"
#include "net_ping.h"
#include "net_wifiriss.h"
#include "traffic_analyzer.hpp"
#include "logger.hpp"
#include <algorithm>

namespace weaknet_dbus {

std::vector<NetInfo> WeakNetMgr::collectCurrentInterfaces() {
    LOG_INFO(LogModule::WEAK_MGR, "collectCurrentInterfaces begin");
    std::vector<NetInfo> result;
    auto mgr = NetInterfaceManager::getInstance();
    auto names = mgr->getInternetInterfaces();
    result.reserve(names.size());
    for (const auto& n : names) {
        NetInfo info(n);
        // 这里简单估计：默认路由网卡设置为 true 的可在将来拓展；当前未知类型、UP 状态
        info.setDefaultRoute(false);
        info.setType(NetType::Unknown);
        info.setState(NetState::Up);
        info.setRttMs(-1);
        result.push_back(info);
    }
    // 标记当前上网网卡（using 标志）
    auto usingMgr = UsingInterfaceManager::getInstance();
    usingMgr->start();
    std::string usingIf = usingMgr->getCurrentInterface();
    for (auto& x : result) x.setUsingNow(!usingIf.empty() && x.ifName() == usingIf);
    LOG_INFO(LogModule::WEAK_MGR, "collectCurrentInterfaces end: " << result.size() << " ifaces, using=" << (usingIf.empty() ? "(none)" : usingIf));
    return result;
}
static LinkQuality classifyQualityFromRtt(int rttMs, int prevMs) {
    (void)prevMs;  // 预留：prevMs 用于后续按 RTT 变化量分级，当前只用绝对阈值
    if (rttMs < 0) return LinkQuality::Bad;
    // 简单阈值，可按需调整
    if (rttMs <= 50) return LinkQuality::Good;
    if (rttMs <= 100) return LinkQuality::Fair;
    if (rttMs <= 200) return LinkQuality::Poor;
    return LinkQuality::Bad;
}

bool WeakNetMgr::updateRttAndState(std::vector<NetInfo>& list, const std::string& host, int timeoutMs) {
    LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: starting, host=" << host << ", timeout=" << timeoutMs << ", size=" << list.size());
    auto pinger = NetPing::getInstance();
    LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: got pinger instance");
    bool anyChanged = false;
    for (auto& x : list) {
        LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: processing interface " << x.ifName());
        int prev = x.rttMs();
        LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: calling ping for " << x.ifName());
        int r = pinger->ping(host, x.ifName(), timeoutMs);
        LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: ping returned " << r << " for " << x.ifName());
        x.setPrevRttMs(prev);
        x.setRttMs(r);
        LinkQuality q = classifyQualityFromRtt(r, prev);
        if (x.quality() != q) { x.setQuality(q); anyChanged = true; }
        // 状态根据 RTT 粗略判断（可扩展更多信号）
        NetState ns = (r >= 0) ? NetState::Up : NetState::Down;
        if (x.state() != ns) { x.setState(ns); anyChanged = true; }
        LOG_INFO(LogModule::WEAK_MGR, "iface=" << x.ifName() << " rtt=" << r << "ms quality=" << static_cast<int>(x.quality()) << " state=" << static_cast<int>(x.state()));
    }
    LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: completed, anyChanged=" << anyChanged);
    return anyChanged;
}

bool WeakNetMgr::updateWifiRssi(std::vector<NetInfo>& list, const std::string& ctrlDir) {
    LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: starting, ctrlDir=" << ctrlDir << " size=" << list.size());
    auto client = WiFiRssiClient::getInstance();
    LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: got client instance");
    bool anyChanged = false;
    for (auto& x : list) {
        LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: processing interface " << x.ifName() << " type=" << static_cast<int>(x.type()));
        if (x.type() != NetType::WiFi) {
            LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: skipping non-WiFi interface " << x.ifName());
            continue;
        }
        // 若尚未连接，尝试连接 ctrl 套接字
        LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: attempting to connect to " << x.ifName());
        if (!client->connect(x.ifName(), ctrlDir)) {
            LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: failed to connect to " << x.ifName());
            continue;
        }
        LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: connected to " << x.ifName() << ", getting RSSI");
        int rssi = client->getRssi();
        LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: got RSSI " << rssi << " for " << x.ifName());
        if (x.rssiDbm() != rssi) {
            x.setRssiDbm(rssi);
            anyChanged = true;
            if (x.usingNow()) {
                LOG_INFO(LogModule::RSSI, "using iface " << x.ifName() << " RSSI=" << rssi << " dBm");
            }
            LOG_INFO(LogModule::WEAK_MGR, "iface=" << x.ifName() << " rssi=" << rssi);
        }
    }
    LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: completed, anyChanged=" << anyChanged);
    return anyChanged;
}

bool WeakNetMgr::updateCurrentUsing(std::vector<NetInfo>& list, bool printLog, std::string* outIfName, uint32_t* outFlags) {
    auto usingMgr = UsingInterfaceManager::getInstance();
    usingMgr->start();
    std::string usingIf = usingMgr->getCurrentInterface();
    uint32_t flags = usingMgr->getMethodFlags();

    bool changed = false;
    LOG_INFO(LogModule::WEAK_MGR, "updateCurrentUsing: current=" << (usingIf.empty() ? "(none)" : usingIf) << " flags=" << flags);
    for (auto& x : list) {
        bool should = (!usingIf.empty() && x.ifName() == usingIf);
        if (x.usingNow() != should) { x.setUsingNow(should); changed = true; }
    }
    if (outIfName) *outIfName = usingIf;
    if (outFlags) *outFlags = flags;
    if (printLog) {
        if (!usingIf.empty()) {
            LOG_INFO(LogModule::INTERFACE, "current=" << usingIf << " flags=" << 
                ((flags & UsingMethodFlag::IPv4Default) ? "IPv4" : "") << 
                (((flags & UsingMethodFlag::IPv4Default) && (flags & UsingMethodFlag::IPv6Default)) ? "+" : 
                 ((flags & UsingMethodFlag::IPv6Default) ? "IPv6" : "")));
        } else {
            LOG_INFO(LogModule::INTERFACE, "current=(none)");
        }
    }

        if (usingIf.empty() && !list.empty()) {
        for (auto& iface : list) {
            if (iface.ifName() != "lo") {
                usingIf = iface.ifName();
                if (!iface.usingNow()) {
                    iface.setUsingNow(true);
                    changed = true;
                }
                if (outIfName) *outIfName = usingIf;
                if (outFlags) *outFlags = flags;
                LOG_INFO(LogModule::WEAK_MGR, "force set using interface to " << usingIf);
                break;
            }
        }
    }

    return changed;
}

bool WeakNetMgr::findByName(const std::vector<NetInfo>& list, const std::string& ifname, NetInfo* out) const {
    for (const auto& x : list) {
        if (x.ifName() == ifname) { if (out) *out = x; return true; }
    }
    return false;
}

std::vector<std::string> WeakNetMgr::namesOf(const std::vector<NetInfo>& list) {
    std::vector<std::string> names;
    names.reserve(list.size());
    for (const auto& x : list) names.push_back(x.ifName());
    return names;
}

bool WeakNetMgr::updateTcpLossRate(std::vector<NetInfo>& list, 
                                  const std::string& iface_name, 
                                  double loss_rate, 
                                  const std::string& loss_level) {
    bool changed = false;
    LOG_INFO(LogModule::WEAK_MGR, "updateTcpLossRate: iface=" << iface_name << " rate=" << loss_rate << " level=" << loss_level);
    
    for (auto& x : list) {
        if (x.ifName() == iface_name) {
            bool rateChanged = false, levelChanged = false;
            if (x.tcpLossRate() != loss_rate) {
                x.setTcpLossRate(loss_rate);
                rateChanged = true;
            }
            if (x.tcpLossLevel() != loss_level) {
                x.setTcpLossLevel(loss_level);
                levelChanged = true;
            }
            if (rateChanged || levelChanged) {
                changed = true;
                if (x.usingNow()) {
                    LOG_INFO(LogModule::TCP_LOSS, "using iface " << iface_name << " TCP loss rate updated: " << loss_rate << "% (" << loss_level << ")");
                }
                LOG_INFO(LogModule::WEAK_MGR, "iface=" << x.ifName() << " tcp_loss_rate=" << x.tcpLossRate() << " tcp_loss_level=" << x.tcpLossLevel());
            }
            break;
        }
    }
    return changed;
}

// 流量分析相关函数实现
void WeakNetMgr::startTrafficAnalysis(const std::string& interface, int interval_seconds) {
    if (!traffic_analyzer_) {
        traffic_analyzer_ = std::make_shared<TrafficAnalyzer>();
    }
    traffic_analyzer_->start(interface, interval_seconds);
    LOG_INFO(LogModule::WEAK_MGR, "Started traffic analysis for interface: " << interface);
}

void WeakNetMgr::stopTrafficAnalysis() {
    if (traffic_analyzer_) {
        traffic_analyzer_->stop();
        LOG_INFO(LogModule::WEAK_MGR, "Stopped traffic analysis");
    }
}

bool WeakNetMgr::updateTrafficAnalysis(std::vector<NetInfo>& list) {
    if (!traffic_analyzer_ || !traffic_analyzer_->isRunning()) {
        return false;
    }
    
    bool changed = false;
    
    try {
        // 获取当前流量统计
        auto stats = traffic_analyzer_->getCurrentStats();
        
        // 获取Top流量连接
        auto topFlows = traffic_analyzer_->getTopFlows(5, 10);
        
        // 检测异常流量
        auto anomalies = traffic_analyzer_->detectAnomalies(5);
        
        // 更新当前上网网卡的流量信息
        for (auto& net : list) {
            if (net.usingNow()) {
                // 更新流量统计信息
                net.setTrafficStats(stats.totalBps, stats.totalPps, stats.activeFlows);
                
                // 如果有异常流量，记录日志
                if (!anomalies.empty()) {
                    LOG_INFO(LogModule::WEAK_MGR, "Traffic anomalies detected on " << net.ifName() 
                        << ": " << anomalies.size() << " anomalies");
                    for (const auto& anomaly : anomalies) {
                        LOG_INFO(LogModule::WEAK_MGR, "Anomaly: " << anomaly.anomalyType 
                            << " severity: " << (anomaly.severity * 100) << "%");
                    }
                }
                
                LOG_INFO(LogModule::WEAK_MGR, "Updated traffic stats for " << net.ifName() 
                    << ": " << (stats.totalBps / (1024*1024)) << " MB/s, " 
                    << stats.activeFlows << " flows, " << stats.totalPps << " pps");
                
                changed = true;
                break;
            }
        }
        
    } catch (const std::exception& e) {
        LOG_ERROR(LogModule::WEAK_MGR, "Traffic analysis update error: " << e.what());
    }
    
    return changed;
}

std::shared_ptr<TrafficAnalyzer> WeakNetMgr::getTrafficAnalyzer() const {
    return traffic_analyzer_;
}

// 线程安全的接口列表操作方法实现
std::vector<NetInfo> WeakNetMgr::getCurrentInterfaces() const {
    std::lock_guard<std::mutex> lock(iface_mutex_);
    return current_interfaces_;
}

void WeakNetMgr::updateInterfaces(const std::vector<NetInfo>& new_interfaces) {
    std::lock_guard<std::mutex> lock(iface_mutex_);
    current_interfaces_ = new_interfaces;
    LOG_INFO(LogModule::WEAK_MGR, "Updated interfaces list: " << current_interfaces_.size() << " interfaces");
}

bool WeakNetMgr::updateRttAndStateSafe(const std::string& host, int timeoutMs) {
    LOG_INFO(LogModule::WEAK_MGR, "updateRttAndStateSafe: acquiring lock");
    std::lock_guard<std::mutex> lock(iface_mutex_);
    LOG_INFO(LogModule::WEAK_MGR, "updateRttAndStateSafe: lock acquired, calling updateRttAndState");
    bool result = updateRttAndState(current_interfaces_, host, timeoutMs);
    LOG_INFO(LogModule::WEAK_MGR, "updateRttAndStateSafe: updateRttAndState completed, releasing lock");
    return result;
}

bool WeakNetMgr::updateWifiRssiSafe(const std::string& ctrlDir) {
    std::lock_guard<std::mutex> lock(iface_mutex_);
    return updateWifiRssi(current_interfaces_, ctrlDir);
}

bool WeakNetMgr::updateTcpLossRateSafe(const std::string& iface_name, double loss_rate, const std::string& loss_level) {
    std::lock_guard<std::mutex> lock(iface_mutex_);
    return updateTcpLossRate(current_interfaces_, iface_name, loss_rate, loss_level);
}

bool WeakNetMgr::updateTrafficAnalysisSafe() {
    std::lock_guard<std::mutex> lock(iface_mutex_);
    return updateTrafficAnalysis(current_interfaces_);
}

bool WeakNetMgr::updateCurrentUsingSafe() {
    std::lock_guard<std::mutex> lock(iface_mutex_);
    return updateCurrentUsing(current_interfaces_, true);
}

}  // namespace weaknet_dbus


