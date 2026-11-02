// rssi_monitor.hpp
// 启动 Wi-Fi RSSI 监控线程

#pragma once

#include <string>

namespace weaknet_dbus {

class ServerContext;

void start_rssi_monitor_thread(ServerContext* ctx, const std::string& ctrlDir = "");

}  // namespace weaknet_dbus


