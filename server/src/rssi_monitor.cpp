// rssi_monitor.cpp
// 对 Wi-Fi 接口周期获取 RSSI 并更新 WeakNetMgr 内的 NetInfo

#include <thread>
#include <chrono>

#include "server.hpp"
#include "weak_netmgr.hpp"
#include "dbus_service.hpp"
#include "rssi_monitor.hpp"
#include "logger.hpp"

using namespace std::chrono_literals;

namespace weaknet_dbus {

void start_rssi_monitor_thread(ServerContext* ctx, const std::string& ctrlDir) {
    std::thread([ctx, ctrlDir]{
        LOG_INFO(LogModule::RSSI, "RSSI monitor thread started");
        if (!ctx->weak_mgr) ctx->weak_mgr = new WeakNetMgr();
        int loop_count = 0;
        while (ctx->running.load()) {
            loop_count++;
            LOG_INFO(LogModule::RSSI, "RSSI monitor thread running, loop=" << loop_count << ", ctx->running=" << ctx->running.load());
            
            // 直接调用线程安全的RSSI更新方法
            LOG_INFO(LogModule::RSSI, "RSSI monitor: calling updateWifiRssiSafe");
            bool changed = ctx->weak_mgr->updateWifiRssiSafe(ctrlDir);
            LOG_INFO(LogModule::RSSI, "RSSI monitor: updateWifiRssiSafe completed, changed=" << changed);
            
            // 获取当前接口列表用于日志输出
            auto current_interfaces = ctx->weak_mgr->getCurrentInterfaces();
            LOG_INFO(LogModule::RSSI, "RSSI monitor: current interfaces count=" << current_interfaces.size());
            
            // 输出RSSI监控信息
            for (const auto& net : current_interfaces) {
                if (net.type() == NetType::WiFi) {
                    LOG_INFO(LogModule::RSSI, "RSSI_MONITOR: " << net.ifName() 
                        << " | RSSI: " << net.rssiDbm() << "dBm" 
                        << " | Quality: " << static_cast<int>(net.quality())
                        << " | Using: " << (net.usingNow() ? "YES" : "NO"));
                }
            }
            
            if (changed && ctx->service) {
                LOG_INFO(LogModule::RSSI, "WiFi RSSI updated - emitting signal");
                // 异步发送DBus信号，避免阻塞RSSI线程
                std::thread([ctx]() {
                    ctx->service->emitChanged("WiFi RSSI updated", /*counter*/0);
                }).detach();
            } else {
                LOG_INFO(LogModule::RSSI, "RSSI_MONITOR: no changes detected (interfaces: " << current_interfaces.size() << ")");
            }
            std::this_thread::sleep_for(10000ms);
        }
    }).detach();
}

}  // namespace weaknet_dbus


