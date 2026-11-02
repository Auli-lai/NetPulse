// rtt_monitor.hpp
// 启动 RTT 监控线程，周期性调用 WeakNetMgr::updateRttAndState

#pragma once

#include <string>

namespace weaknet_dbus {

class ServerContext;

// 创建并启动 RTT 监控线程
// host: 目标主机（如 1.1.1.1 / 8.8.8.8 / 自定义域名）
void start_rtt_monitor_thread(ServerContext* ctx, const std::string& host, int intervalMs = 2000, int timeoutMs = 800);

}  // namespace weaknet_dbus


