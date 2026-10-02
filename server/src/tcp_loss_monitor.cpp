// tcp_loss_monitor.cpp
// TCP丢包率监控线程实现

#include "server.hpp"
#include "tcp_loss_monitor.hpp"
#include "net_tcp.h"
#include "weak_netmgr.hpp"
#include "dbus_service.hpp"
#include "logger.hpp"
#include <cstdio>
#include <chrono>

using namespace std::chrono_literals;

namespace weaknet_dbus {

// TCP丢包率监控线程函数
void start_tcp_loss_monitor_thread(ServerContext* ctx) {
    ctx->tcp_loss_thread = std::thread([ctx](){
        std::printf("[tcp_loss] monitor thread started.\n");
        
        if (!ctx->weak_mgr) ctx->weak_mgr = new WeakNetMgr();
        
        auto tcpMonitor = TcpLossMonitor::getInstance();
        TcpStats prevStats, currStats;
        bool hasPrevStats = false;
        
        int loop_count = 0;
        while (ctx->running.load()) {
            loop_count++;
            LOG_INFO(LogModule::TCP_LOSS, "tick: monitoring TCP loss rate... (loop=" << loop_count << ")");
            
            // 获取当前上网网卡信息
            auto current_interfaces = ctx->weak_mgr->getCurrentInterfaces();
            std::string currentIface;
            for (const auto& net : current_interfaces) {
                if (net.usingNow()) {
                    currentIface = net.ifName();
                    break;
                }
            }
            
            if (currentIface.empty()) {
                LOG_INFO(LogModule::TCP_LOSS, "TCP_LOSS_MONITOR: no active interface found (checking " << current_interfaces.size() << " interfaces)");
                std::this_thread::sleep_for(5000ms);  // 减少等待时间，更频繁地检查
                continue;
            }
            
            LOG_INFO(LogModule::TCP_LOSS, "monitoring interface: " << currentIface);
            
            // 采样当前TCP统计信息
            if (!tcpMonitor->sampleForInterface(currentIface, currStats)) {
                LOG_ERROR(LogModule::TCP_LOSS, "failed to sample TCP stats for interface: " << currentIface);
                std::this_thread::sleep_for(10000ms);
                continue;
            }
            
            // 计算丢包率（如果有之前的统计数据）
            if (hasPrevStats) {
                TcpLossResult result = tcpMonitor->compute(prevStats, currStats);
                
                if (result.sentDelta >= 10) {  // 只有当发送的数据包超过阈值时才计算
                    LOG_INFO(LogModule::TCP_LOSS, "TCP_LOSS_MONITOR: interface=" << currentIface 
                        << " rate=" << result.ratePercent << "%" 
                        << " delta_sent=" << result.sentDelta 
                        << " delta_retrans=" << result.retransDelta 
                        << " level=" << result.level);
                    
                    // 更新到weak_mgr中保存的NetInfo列表
                    bool updated = ctx->weak_mgr->updateTcpLossRateSafe(currentIface, result.ratePercent, result.level);
                    if (updated && ctx->service) {
                        std::string msg = std::string("TCP loss rate updated for ") + currentIface + 
                                         ": " + std::to_string(result.ratePercent) + "% (" + result.level + ")";
                        LOG_INFO(LogModule::TCP_LOSS, "TCP loss rate updated - emitting signal: " << msg);
                        // 异步发送DBus信号，避免阻塞TCP丢包率线程
                        std::thread([ctx, msg]() {
                            ctx->service->emitChanged(msg, /*counter*/0);
                        }).detach();
                    }
                }
            }
            
            // 保存当前统计作为下次的前次统计
            prevStats = currStats;
            hasPrevStats = true;
            
            std::this_thread::sleep_for(10000ms);
        }
        
        std::printf("[tcp_loss] monitor thread terminated.\n");
    });
}

} // namespace weaknet_dbus
