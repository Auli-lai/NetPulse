// server.hpp
// 声明启动 DBus 服务端的入口函数，供 main 调用

#pragma once

#include <vector>
#include <string>
#include <mutex>
#include <thread>
#include <atomic>

// 前置声明，避免强依赖 dbus 头
struct DBusConnection;

namespace weaknet_dbus {

class DbusService;  // 前置声明
class WeakNetMgr;   // 前置声明
class NetInfo;      // 前置声明

struct ServerContext {
    // DBus 连接
    ::DBusConnection* connection = nullptr;

    // 运行标志
    std::atomic<bool> running{true};

    // 网卡监控线程
    std::thread iface_thread;
    // 当前上网网卡监控线程
    std::thread using_thread;
    // RTT 监控线程（detach 管理，保留占位以便未来 join）
    std::thread rtt_thread;
    // TCP丢包率监控线程
    std::thread tcp_loss_thread;
    // 流量分析线程
    std::thread traffic_analysis_thread;
    // 网络质量监控线程
    std::thread network_quality_thread;

    // 共享的可上网网卡列表（NetInfo）
    std::mutex iface_mutex;
    std::vector<NetInfo> iface_list;

    // 服务对象（方法处理与信号发送）
    DbusService* service = nullptr;

    // 弱网管理器
    WeakNetMgr* weak_mgr = nullptr;
};

// 初始化 DBus（线程支持、连接、请求服务名、注册对象路径与回调）
// 返回连接指针，失败返回 nullptr
::DBusConnection* init_dbus(ServerContext* ctx);

// 启动网卡监控线程（维护接口列表并发送 Changed 信号）
void start_iface_monitor_thread(ServerContext* ctx);

// 启动流量分析线程（内部函数）
// void start_traffic_analysis_thread(ServerContext* ctx);

// 启动服务：
// - 创建 DBus 连接、注册对象与方法
// - 启动网卡监控线程，实时维护具备上网能力的网卡列表
// - 主线程运行 Looper::current()->run() 阻塞，避免程序退出
// 该函数会阻塞，直到进程被外部信号终止
int start_server();

}  // namespace weaknet_dbus


