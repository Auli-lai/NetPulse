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

// 把 Windows 宿主机 WiFi 快照灌进 NetInfo（合成 wlan0 的注入与周期刷新共用）。
// 与 agent/host_bridge.py 的 _apply_rates 之后的数据形态保持一致：
// 速率/抖动由桥接内部按两次采样差值算出，这里只负责落字段。
static void applyHostWifiSnapshot(NetInfo& wifi, const WiFiRssiClient::WindowsHostWifiSnapshot& s) {
    wifi.setType(NetType::WiFi);
    wifi.setState(NetState::Up);
    wifi.setRssiDbm(s.rssiDbm);
    wifi.setSsid(s.ssid);
    wifi.setRttMs(s.rttMs);
    wifi.setRttSamples(s.rttSamples);
    wifi.setJitterMs(s.jitterMs);
    wifi.setDownloadBps(static_cast<uint64_t>(s.downloadBps));
    wifi.setUploadBps(static_cast<uint64_t>(s.uploadBps));
    wifi.setHasTraffic(s.downloadBps > 0.0 || s.uploadBps > 0.0);
    wifi.setLinkRxMbps(s.linkRxMbps);
    wifi.setLinkTxMbps(s.linkTxMbps);
    wifi.setHostWifiSynthetic(true);
    wifi.setTcpLossRate(s.tcpLossPct);
    wifi.setTcpLossLevel(s.tcpLossPct <= 0.5 ? "good" : (s.tcpLossPct <= 3.0 ? "degraded" : "poor"));
    // 流量统计位复用：把桥接测得的上下行速率放进 totalBps，
    // 让 NetworkQualityAssessor 的流量分有真实数据可用（pps/flows 无从得知，置 0）
    wifi.setTrafficStats(static_cast<uint64_t>(s.downloadBps + s.uploadBps), 0, 0);
    wifi.setQuality(WeakNetMgr::classifyQualityFromRtt(s.rttMs));
}

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

    // WSL 桥接：Linux 侧看不到宿主 WiFi 时，尝试通过 Windows netsh 读取并注入 wlan0。
    // 注意：这个 wlan0 只承载宿主机 WiFi 的信息（RSSI/SSID/RTT/速率/丢包），
    // eBPF 流量与 Linux 侧 RTT 监控仍然以默认网卡（eth0）为对象——
    // 合成网卡 hostWifiSynthetic() 为 true，各更新循环据此跳过它，
    // 防止 NetPing 对不存在的 wlan0 返回 -2 把桥接数据清掉。
    {
        const bool hasWlan0 = std::any_of(result.begin(), result.end(),
            [](const NetInfo& x) { return x.ifName() == "wlan0"; });
        WiFiRssiClient::WindowsHostWifiSnapshot snapshot;
        // 有界查询：内部带缓存复用（5s）+ 突发合并，慢查询甩到工作线程，
        // 本线程最多等 15 秒。**不能在这里同步等一个没有上限的查询**——
        // 这个函数运行在主线程的启动路径上（server.cpp 进 D-Bus 循环前），
        // 一挂整个服务就不应答（真实事故：WSL 里裸 ping 无限期执行）。
        // 等不到就跳过本轮注入，工作线程查到后由下一个周期补上。
        const bool got = WiFiRssiClient::queryWindowsHostWifiSnapshotBounded(snapshot, /*maxWaitSec=*/15.0);
        if (!got) {
            LOG_INFO(LogModule::WEAK_MGR,
                     "host WiFi bridge: no snapshot yet (query slow/failed), skipping injection this round");
        }
        if (!hasWlan0 && got) {
            NetInfo wifi("wlan0");
            applyHostWifiSnapshot(wifi, snapshot);
            storeHostWifiSnapshot(snapshot);
            // 宿主机 WiFi 才是真正的上网出口，必须插到列表最前并标记 using：
            // NetworkQualityAssessor / tcp_loss_monitor / dbus_service 都取
            // "第一个 usingNow 网卡"作为上报主体，顺序与标志缺一不可
            // （放在末尾时这些模块会继续拿 eth0 当主体，桥接就白做了）。
            result.insert(result.begin(), wifi);
            LOG_INFO(LogModule::WEAK_MGR,
                     "injected Windows host WiFi: adapter=" << snapshot.adapterName
                     << ", SSID=" << snapshot.ssid
                     << ", RSSI=" << snapshot.rssiDbm << "dBm"
                     << ", band=" << snapshot.band << " ch=" << snapshot.channel
                     << ", RTT=" << snapshot.rttMs << "ms"
                     << ", jitter=" << snapshot.jitterMs << "ms"
                     << ", down=" << snapshot.downloadBps << "B/s"
                     << ", up=" << snapshot.uploadBps << "B/s"
                     << ", loss=" << snapshot.tcpLossPct << "%"
                     << ", state=" << snapshot.state);
        }
    }

    // 标记当前上网网卡（using 标志）。
    // 合成 wlan0 代表宿主机真实出口，始终保留 using：Linux 侧 using 判定
    // 只能看到 eth0，直接覆盖会把桥接网卡的标志清掉，下游模块随之失去主体。
    auto usingMgr = UsingInterfaceManager::getInstance();
    usingMgr->start();
    std::string usingIf = usingMgr->getCurrentInterface();
    for (auto& x : result) {
        x.setUsingNow(x.hostWifiSynthetic() || (!usingIf.empty() && x.ifName() == usingIf));
    }
    LOG_INFO(LogModule::WEAK_MGR, "collectCurrentInterfaces end: " << result.size() << " ifaces, using=" << (usingIf.empty() ? "(none)" : usingIf));
    return result;
}

LinkQuality WeakNetMgr::classifyQualityFromRtt(int rttMs) {
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
        // 合成 wlan0（WSL 宿主 WiFi 桥接）在 Linux 侧没有对应设备，
        // NetPing 绑定该网卡必然失败返回 -2，会把桥接测得的 RTT 清掉，
        // 它的 RTT 由 updateWifiRssi 走 Windows 桥接更新，这里必须跳过。
        if (x.hostWifiSynthetic()) {
            LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: skipping host-bridged interface " << x.ifName());
            continue;
        }
        LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: processing interface " << x.ifName());
        int prev = x.rttMs();
        LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: calling ping for " << x.ifName());
        int r = pinger->ping(host, x.ifName(), timeoutMs);
        LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: ping returned " << r << " for " << x.ifName());
        x.setPrevRttMs(prev);
        x.setRttMs(r);
        LinkQuality q = classifyQualityFromRtt(r);
        if (x.quality() != q) { x.setQuality(q); anyChanged = true; }
        // 状态根据 RTT 粗略判断（可扩展更多信号）
        NetState ns = (r >= 0) ? NetState::Up : NetState::Down;
        if (x.state() != ns) { x.setState(ns); anyChanged = true; }
        LOG_INFO(LogModule::WEAK_MGR, "iface=" << x.ifName() << " rtt=" << r << "ms quality=" << static_cast<int>(x.quality()) << " state=" << static_cast<int>(x.state()));
    }
    LOG_INFO(LogModule::WEAK_MGR, "updateRttAndState: completed, anyChanged=" << anyChanged);
    return anyChanged;
}

bool WeakNetMgr::updateWifiRssi(std::vector<NetInfo>& list, const std::string& ctrlDir,
                                const WiFiRssiClient::WindowsHostWifiSnapshot* preFetched) {
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
        // 原生 Linux：走 wpa_supplicant；WSL 下的合成 wlan0：走 Windows 宿主机桥接。
        int rssi = -1000;
        if (x.hostWifiSynthetic()) {
            // 合成网卡在 Linux 侧根本不存在，wpa_supplicant 必然连不上，直接走桥接
            WiFiRssiClient::WindowsHostWifiSnapshot local;
            const WiFiRssiClient::WindowsHostWifiSnapshot* snapshot = preFetched;
            if (snapshot && !snapshot->ok) {
                // 调用方（updateWifiRssiSafe）已经查过且失败：不能拿空快照
                // 覆盖已有数据，也别再查一遍（查询要 3~4 秒）
                LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: pre-fetched bridge query failed, keeping last data");
                continue;
            }
            if (!snapshot) {
                LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: " << x.ifName() << " is host-bridged, querying Windows snapshot");
                if (!WiFiRssiClient::queryWindowsHostWifiSnapshot(local)) {
                    LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: Windows host WiFi bridge unavailable");
                    continue;
                }
                snapshot = &local;
            }
            applyHostWifiSnapshot(x, *snapshot);
            storeHostWifiSnapshot(*snapshot);
            rssi = snapshot->rssiDbm;
            LOG_INFO(LogModule::WEAK_MGR,
                     "updateWifiRssi: using Windows host WiFi bridge, adapter=" << snapshot->adapterName
                     << ", SSID=" << snapshot->ssid
                     << ", RSSI=" << rssi << "dBm"
                     << ", RTT=" << snapshot->rttMs << "ms"
                     << ", jitter=" << snapshot->jitterMs << "ms"
                     << ", down=" << snapshot->downloadBps << "B/s"
                     << ", up=" << snapshot->uploadBps << "B/s"
                     << ", loss=" << snapshot->tcpLossPct << "%");
        } else if (client->connect(x.ifName(), ctrlDir)) {
            LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: connected to " << x.ifName() << ", getting RSSI");
            rssi = client->getRssi();
        } else {
            LOG_INFO(LogModule::WEAK_MGR, "updateWifiRssi: failed to connect to " << x.ifName());
            continue;
        }
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
        // 合成 wlan0（宿主机 WiFi 桥接）始终保留 using：Linux 侧 using 判定
        // 只能看到 eth0，不保住这个标志的话，下游"取第一个 usingNow 网卡"的
        // 模块（NetworkQualityAssessor / tcp_loss_monitor / dbus_service）
        // 每一轮都会把上报主体切回 eth0。
        bool should = x.hostWifiSynthetic() || (!usingIf.empty() && x.ifName() == usingIf);
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
            // 跳过合成 wlan0：eBPF 采样挂在 Linux 默认网卡（eth0）上，
            // 桥接网卡的流量来自 Windows 网卡计数器（见 applyHostWifiSnapshot），
            // 不能拿 eth0 的 eBPF 数据覆盖宿主 WiFi 的真实速率。
            if (net.usingNow() && !net.hostWifiSynthetic()) {
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

void WeakNetMgr::storeHostWifiSnapshot(const WiFiRssiClient::WindowsHostWifiSnapshot& s) {
    std::lock_guard<std::mutex> lock(host_wifi_mutex_);
    host_wifi_snapshot_ = s;
}

WiFiRssiClient::WindowsHostWifiSnapshot WeakNetMgr::lastHostWifiSnapshot() const {
    std::lock_guard<std::mutex> lock(host_wifi_mutex_);
    return host_wifi_snapshot_;
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
    // WSL 桥接查询（popen netsh/ping/powershell，3~4 秒）放在锁外：
    // 持锁查询会把 handleHealthCheck / handleListInterfaces 一起堵死好几秒。
    WiFiRssiClient::WindowsHostWifiSnapshot snapshot;
    bool needBridge = false;
    {
        std::lock_guard<std::mutex> lock(iface_mutex_);
        needBridge = std::any_of(current_interfaces_.begin(), current_interfaces_.end(),
                                 [](const NetInfo& x) { return x.hostWifiSynthetic(); });
    }
    if (needBridge) {
        // 有界查询（内部串行化：全局同时最多一个真实查询在跑；别的线程
        // 正在查时就搭便车等它，不会重复起 ping/powershell）。
        // 失败时 snapshot.ok 仍为 false：updateWifiRssi 据此跳过而不是重复查询。
        // 这是后台线程，等久一点没关系。
        WiFiRssiClient::queryWindowsHostWifiSnapshotBounded(snapshot, /*maxWaitSec=*/20.0);
    }
    std::lock_guard<std::mutex> lock(iface_mutex_);
    return updateWifiRssi(current_interfaces_, ctrlDir, needBridge ? &snapshot : nullptr);
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


