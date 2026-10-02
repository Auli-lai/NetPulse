// server.cpp
// 使用 libdbus-1 提供服务端：导出 Get 方法与 Changed 信号；并提供 start_server 作为入口

#include <dbus/dbus.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>
#include <thread>
#include <chrono>
#include <atomic>
#include <algorithm>
#include <mutex>

#include "common.hpp"
#include "serializer.hpp"
#include "net_iface.h"
#include "server.hpp"
#include "dbus_service.hpp"
#include "looper.hpp"
#include "net_info.hpp"
#include "weak_netmgr.hpp"
#include "rtt_monitor.hpp"
#include "rssi_monitor.hpp"
#include "tcp_loss_monitor.hpp"
#include "event_manager.hpp"
#include "logger.hpp"
#include "network_quality_assessor.hpp"
#include <iomanip>

using namespace std::chrono_literals;

namespace weaknet_dbus {

// 共享列表迁移至 ServerContext，在 server.hpp 中定义

// 使用 DbusService 替代手写处理函数

// 将字符串作为方法返回，通过序列化保存到文件
// Get 方法处理已迁移到 DbusService

// 发送 Changed 信号，并将载荷序列化到文件
// 发信号也迁移到 DbusService

// 处理进入总线的消息（方法调用等）
// 消息处理迁移，由 DbusService::MessageHandler 提供

// 字符串数组回复已封装到 DbusService

// 比较两个列表，打印新增与删除项，并返回是否有变化
static bool diffInterfaces(const std::vector<std::string>& old_list,
                           const std::vector<std::string>& new_list,
                           std::vector<std::string>& added,
                           std::vector<std::string>& removed) {
    added.clear();
    removed.clear();

    for (const auto& it : new_list) {
        if (std::find(old_list.begin(), old_list.end(), it) == old_list.end()) added.push_back(it);
    }
    for (const auto& it : old_list) {
        if (std::find(new_list.begin(), new_list.end(), it) == new_list.end()) removed.push_back(it);
    }
    return !added.empty() || !removed.empty();
}

// 独立接口：初始化 DBus、注册对象路径
DBusConnection* init_dbus(ServerContext* ctx) {
    LOG_INFO(LogModule::DBUS, "init_dbus: start connecting to session bus...");
    dbus_threads_init_default();

    DBusError err;
    dbus_error_init(&err);

    DBusConnection* conn = dbus_bus_get(DBUS_BUS_SESSION, &err);
    if (dbus_error_is_set(&err)) {
        LOG_ERROR(LogModule::DBUS, "连接总线失败: " << err.message);
        dbus_error_free(&err);
    }
    if (!conn) return nullptr;
    LOG_INFO(LogModule::DBUS, "connected to session bus");

    LOG_INFO(LogModule::DBUS, "requesting bus name: " << kBusName);
    int ret = dbus_bus_request_name(conn, kBusName, DBUS_NAME_FLAG_REPLACE_EXISTING, &err);
    if (dbus_error_is_set(&err)) {
        LOG_ERROR(LogModule::DBUS, "请求服务名失败: " << err.message);
        dbus_error_free(&err);
    }
    if (ret != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        LOG_ERROR(LogModule::DBUS, "未能成为主拥有者，ret=" << ret);
        return nullptr;
    }

    // 使用服务类进行对象注册（保存到上下文，统一管理生命周期）
    LOG_INFO(LogModule::DBUS, "registering object path: " << kObjectPath << " (interface=" << kInterface << ")");
    ctx->service = new DbusService(ctx);
    if (!ctx->service->register_on_connection(conn)) {
        LOG_ERROR(LogModule::DBUS, "注册对象路径失败");
        delete ctx->service;
        ctx->service = nullptr;
        return nullptr;
    }
    // 指针已保存至 ctx

    ctx->connection = conn;
    LOG_INFO(LogModule::DBUS, "DBus 服务端已启动，接口 " << kInterface << "，方法 " << kMethodGet << "，信号 " << kSignalChanged);
    return conn;
}

// 独立接口：启动网卡监控线程（使用 WeakNetMgr 与 NetInfo）
void start_iface_monitor_thread(ServerContext* ctx) {
    ctx->iface_thread = std::thread([ctx](){
        LOG_INFO(LogModule::INTERFACE, "monitor thread started");
        if (!ctx->weak_mgr) ctx->weak_mgr = new WeakNetMgr();
        std::vector<NetInfo> current;
        int32_t change_counter = 0;

        while (ctx->running.load()) {
            LOG_INFO(LogModule::INTERFACE, "tick: collecting interfaces...");
            std::vector<NetInfo> latest = ctx->weak_mgr->collectCurrentInterfaces();
            LOG_INFO(LogModule::INTERFACE, "collected " << latest.size() << " interfaces");
            
            // 持续输出关键指标信息
            for (const auto& net : latest) {
                if (net.usingNow()) {
                    LOG_INFO(LogModule::INTERFACE, "ACTIVE: " << net.ifName() 
                        << " | RTT: " << net.rttMs() << "ms" 
                        << " | Quality: " << static_cast<int>(net.quality())
                        << " | RSSI: " << net.rssiDbm() << "dBm"
                        << " | TCP Loss: " << net.tcpLossRate() << "% (" << net.tcpLossLevel() << ")"
                        << " | Traffic: " << (net.trafficTotalBps() / (1024*1024)) << "MB/s, " 
                        << net.trafficActiveFlows() << " flows, " << net.trafficTotalPps() << " pps");
                } else {
                    LOG_INFO(LogModule::INTERFACE, "INACTIVE: " << net.ifName() 
                        << " | RTT: " << net.rttMs() << "ms" 
                        << " | Quality: " << static_cast<int>(net.quality())
                        << " | RSSI: " << net.rssiDbm() << "dBm"
                        << " | TCP Loss: " << net.tcpLossRate() << "% (" << net.tcpLossLevel() << ")");
                }
            }
            auto old_names = WeakNetMgr::namesOf(current);
            auto new_names = WeakNetMgr::namesOf(latest);
            std::vector<std::string> added, removed;
            if (diffInterfaces(old_names, new_names, added, removed)) {
                current = latest;
                // 使用线程安全的方法更新接口列表
                ctx->weak_mgr->updateInterfaces(current);
                std::string msg = "Interfaces changed (using flags in log): +";
                for (size_t i = 0; i < added.size(); ++i) { msg += (i == 0 ? "" : ","); msg += added[i]; }
                msg += " -";
                for (size_t i = 0; i < removed.size(); ++i) { msg += (i == 0 ? "" : ","); msg += removed[i]; }
                LOG_INFO(LogModule::INTERFACE, msg);
                // 打印 using 标志
                for (const auto& x : current) {
                    if (x.usingNow()) {
                        LOG_INFO(LogModule::INTERFACE, "[using] " << x.ifName() << " is current uplink");
                    }
                }
                // 异步发送DBus信号，避免阻塞接口监控线程
                if (ctx->service) {
                    std::thread([ctx, msg, change_counter]() {
                        ctx->service->emitChanged(msg, change_counter);
                        // 发送网卡变化事件
                        getEventManager().emitInterfaceChanged(msg, "network_manager");
                    }).detach();
                }
            } else {
                LOG_INFO(LogModule::INTERFACE, "no changes detected");
            }
            std::this_thread::sleep_for(10000ms);
        }
    });
}

// 独立接口：启动流量分析线程
static void start_traffic_analysis_thread(ServerContext* ctx) {
    ctx->traffic_analysis_thread = std::thread([ctx](){
        LOG_INFO(LogModule::WEAK_MGR, "traffic analysis thread started");
        if (!ctx->weak_mgr) ctx->weak_mgr = new WeakNetMgr();
        
        // 启动流量分析器：优先读取 WEAKNET_IFACE 环境变量，默认 eth0
        const char* monitor_iface = std::getenv("WEAKNET_IFACE");
        if (!monitor_iface || !*monitor_iface) monitor_iface = "eth0";
        LOG_INFO(LogModule::WEAK_MGR, "traffic analysis monitoring interface: " << monitor_iface);
        ctx->weak_mgr->startTrafficAnalysis(monitor_iface, 10);
        
        int loop_count = 0;
        while (ctx->running.load()) {
            loop_count++;
            LOG_INFO(LogModule::WEAK_MGR, "traffic analysis thread running, loop=" << loop_count);
            try {
                // 直接调用线程安全的流量分析更新方法
                LOG_INFO(LogModule::WEAK_MGR, "traffic analysis: calling updateTrafficAnalysisSafe");
                bool changed = ctx->weak_mgr->updateTrafficAnalysisSafe();
                LOG_INFO(LogModule::WEAK_MGR, "traffic analysis: updateTrafficAnalysisSafe completed, changed=" << changed);
                
                // 获取当前接口列表用于日志输出
                auto current_interfaces = ctx->weak_mgr->getCurrentInterfaces();
                LOG_INFO(LogModule::WEAK_MGR, "traffic analysis: current interfaces count=" << current_interfaces.size());
                
                if (changed && ctx->service) {
                    LOG_INFO(LogModule::WEAK_MGR, "Traffic analysis updated - emitting signal");
                    // 异步发送DBus信号，避免阻塞流量分析线程
                    std::thread([ctx]() {
                        ctx->service->emitChanged("Traffic analysis updated", /*counter*/0);
                    }).detach();
                } else {
                    LOG_INFO(LogModule::WEAK_MGR, "TRAFFIC_ANALYSIS: no changes detected (interfaces: " << current_interfaces.size() << ")");
                }
            } catch (const std::exception& e) {
                LOG_ERROR(LogModule::WEAK_MGR, "Traffic analysis error: " << e.what());
            }
            
            std::this_thread::sleep_for(10000ms);
        }
        
        // 停止流量分析器
        ctx->weak_mgr->stopTrafficAnalysis();
        LOG_INFO(LogModule::WEAK_MGR, "traffic analysis thread stopped");
    });
}

// 独立接口：启动"当前上网网卡"监控线程（使用 UsingInterfaceManager）
static void start_using_iface_thread(ServerContext* ctx) {
    ctx->using_thread = std::thread([ctx](){
        LOG_INFO(LogModule::WEAK_MGR, "monitor thread started");
        if (!ctx->weak_mgr) ctx->weak_mgr = new WeakNetMgr();
        
        int loop_count = 0;
        while (ctx->running.load()) {
            loop_count++;
            LOG_INFO(LogModule::WEAK_MGR, "using iface thread running, loop=" << loop_count);
            
            // 直接调用线程安全的当前使用接口更新方法
            LOG_INFO(LogModule::WEAK_MGR, "using iface: calling updateCurrentUsingSafe");
            bool changed = ctx->weak_mgr->updateCurrentUsingSafe();
            LOG_INFO(LogModule::WEAK_MGR, "using iface: updateCurrentUsingSafe completed, changed=" << changed);
            
            // 获取当前接口列表用于日志输出
            auto current_interfaces = ctx->weak_mgr->getCurrentInterfaces();
            LOG_INFO(LogModule::WEAK_MGR, "using iface: current interfaces count=" << current_interfaces.size());
            
            if (changed && ctx->service) {
                // 查找当前使用的接口
                std::string currentIf;
                for (const auto& net : current_interfaces) {
                    if (net.usingNow()) {
                        currentIf = net.ifName();
                        break;
                    }
                }
                
                std::string msg = std::string("Using iface updated: ") + (currentIf.empty() ? "(none)" : currentIf);
                // 异步发送DBus信号，避免阻塞当前使用接口监控线程
                std::thread([ctx, msg, currentIf]() {
                    ctx->service->emitChanged(msg, /*counter*/0);
                    // 发送上网方式变化事件
                    getEventManager().emitConnectionModeChanged(msg, currentIf.empty() ? "none" : currentIf);
                }).detach();
            } else {
                LOG_INFO(LogModule::WEAK_MGR, "unchanged (interfaces: " << current_interfaces.size() << ")");
            }
            std::this_thread::sleep_for(10000ms);
        }
    });
}

// 独立接口：启动网络质量监控线程
static void start_network_quality_thread(ServerContext* ctx) {
    ctx->network_quality_thread = std::thread([ctx](){
        LOG_INFO(LogModule::WEAK_MGR, "network quality monitor thread started");
        
        NetworkQualityAssessor assessor;
        NetworkQualityResult lastQuality;
        lastQuality.level = NetworkQualityLevel::UNKNOWN;
        
        int loop_count = 0;
        while (ctx->running.load()) {
            loop_count++;
            LOG_INFO(LogModule::WEAK_MGR, "network quality thread running, loop=" << loop_count);
            try {
                // 直接获取当前接口列表（线程安全）
                auto currentInterfaces = ctx->weak_mgr->getCurrentInterfaces();
                LOG_INFO(LogModule::WEAK_MGR, "network quality: current interfaces count=" << currentInterfaces.size());
                
                // 评估网络质量
                NetworkQualityResult currentQuality = assessor.assessQuality(currentInterfaces);
                
                // 检查质量是否发生变化
                if (currentQuality.level != lastQuality.level || 
                    std::abs(currentQuality.score - lastQuality.score) > 15.0) {
                    
                    LOG_INFO(LogModule::WEAK_MGR, "网络质量变化: " << currentQuality.levelName 
                        << " (分数: " << std::fixed << std::setprecision(1) << currentQuality.score << ")");
                    
                    // 发送网络质量变化事件
                    getEventManager().emitNetworkQualityChanged(
                        currentQuality.levelName, 
                        currentQuality.details, 
                        "network_quality_assessor"
                    );
                    
                    lastQuality = currentQuality;
                } else {
                    LOG_INFO(LogModule::WEAK_MGR, "网络质量稳定: " << currentQuality.levelName 
                        << " (分数: " << std::fixed << std::setprecision(1) << currentQuality.score << ")");
                }
                
            } catch (const std::exception& e) {
                LOG_ERROR(LogModule::WEAK_MGR, "网络质量监控错误: " << e.what());
            }
            
            std::this_thread::sleep_for(15000ms);  // 15秒检查一次
        }
        
        LOG_INFO(LogModule::WEAK_MGR, "network quality monitor thread stopped");
    });
}

// 启动服务
int start_server() {
    // 初始化日志系统
    if (!Logger::init("server", "./logs/server", LogLevel::INFO, true)) {
        std::cerr << "Failed to initialize logger" << std::endl;
        return 1;
    }
    
    ServerContext ctx;
    if (!init_dbus(&ctx)) return 1;
    
    // 启动事件监控
    getEventManager().startEventMonitoring(&ctx);
    
    // 初始化WeakNetMgr
    if (!ctx.weak_mgr) ctx.weak_mgr = new WeakNetMgr();
    
    // 初始化接口列表到WeakNetMgr中
    LOG_INFO(LogModule::WEAK_MGR, "initializing interface list...");
    auto initial_interfaces = ctx.weak_mgr->collectCurrentInterfaces();
    ctx.weak_mgr->updateInterfaces(initial_interfaces);
    LOG_INFO(LogModule::WEAK_MGR, "interface list initialized with " << initial_interfaces.size() << " interfaces");
    
    start_iface_monitor_thread(&ctx);
    start_using_iface_thread(&ctx);
    // 启动 RTT 监控线程：使用阿里云 DNS 223.5.5.5 作为目标
    LOG_INFO(LogModule::RTT, "starting monitor thread (target=223.5.5.5, interval=10s)");
    start_rtt_monitor_thread(&ctx, "223.5.5.5", /*intervalMs*/10000, /*timeoutMs*/800);
    // 启动 Wi-Fi RSSI 监控线程（wpa_supplicant ctrl 目录自动探测）
    LOG_INFO(LogModule::RSSI, "starting RSSI monitor thread (interval=10s)");
    start_rssi_monitor_thread(&ctx);
    // 启动 TCP 丢包率监控线程
    LOG_INFO(LogModule::TCP_LOSS, "starting TCP loss rate monitor thread (interval=10s)");
    start_tcp_loss_monitor_thread(&ctx);
    // 启动流量分析线程
    LOG_INFO(LogModule::WEAK_MGR, "starting traffic analysis thread (interval=10s)");
    start_traffic_analysis_thread(&ctx);
    // 启动网络质量监控线程
    LOG_INFO(LogModule::WEAK_MGR, "starting network quality monitor thread (interval=15s)");
    start_network_quality_thread(&ctx);
    // 主线程进入阻塞式 looper
    auto* lp = Looper::current();
    lp->attach(ctx.connection);
    lp->run(&ctx);
    // 按当前要求，服务常驻不退出，以下代码不会到达；保留以备扩展
   // ctx.iface_thread.join();
   // ctx.tcp_loss_thread.join();
  //  ctx.traffic_analysis_thread.join();
    
    // 清理glog
    google::ShutdownGoogleLogging();
    
    return 0;
}

}  // namespace weaknet_dbus



