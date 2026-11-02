// event_manager.hpp
// 事件管理器：处理网卡更换和上网方式改变事件

#pragma once

#include <string>
#include <vector>
#include <chrono>
#include <functional>
#include <memory>
#include "net_info.hpp"

namespace weaknet_dbus {

// 事件类型枚举
enum class EventType {
    InterfaceChanged,        // 网卡变化（添加/移除）
    ConnectionModeChanged,   // 上网方式改变
    NetworkQualityChanged,   // 网络质量变化
    TcpLossRateChanged,      // TCP丢包率变化
    RttChanged,             // RTT变化
    RssiChanged             // RSSI变化
};

// 事件数据结构
struct NetworkEvent {
    EventType type;
    std::string message;
    std::chrono::system_clock::time_point timestamp;
    std::string source;              // 触发源（网卡名/服务名等）
    std::string details;            // 详细信息（JSON格式可选）
    int32_t priority = 0;           // 优先级（0-10，10最高）
    
    NetworkEvent() = default;
    NetworkEvent(EventType t, const std::string& msg, const std::string& src = "", int32_t prio = 0)
        : type(t), message(msg), timestamp(std::chrono::system_clock::now()), source(src), priority(prio) {}
};

// 事件回调函数类型
using EventCallback = std::function<void(const NetworkEvent&)>;

// 前向声明，避免循环依赖
struct ServerContext;

// 事件管理器类
class NetworkEventManager {
public:
    NetworkEventManager();
    ~NetworkEventManager() = default;

    // 注册事件回调
    void registerCallback(EventType type, EventCallback callback);
    
    // 注销回调
    void unregisterCallback(EventType type);
    
    // 发送事件
    void emitEvent(const NetworkEvent& event);
    
    // 便捷的事件发送方法
    void emitInterfaceChanged(const std::string& message, const std::string& source = "");
    void emitConnectionModeChanged(const std::string& message, const std::string& source = "");
    void emitNetworkQualityChanged(const std::string& message, const std::string& details = "", const std::string& source = "");
    void emitTcpLossRateChanged(const std::string& message, const std::string& source = "");
    void emitRttChanged(const std::string& message, const std::string& source = "");
    void emitRssiChanged(const std::string& message, const std::string& source = "");
    
    // 启动事件监控（集成到ServerContext中）
    void startEventMonitoring(struct ServerContext* ctx);
    
    // 停止事件监控
    void stopEventMonitoring();

private:
    std::vector<EventCallback> interface_callbacks_;
    std::vector<EventCallback> connection_mode_callbacks_;
    std::vector<EventCallback> network_quality_callbacks_;
    std::vector<EventCallback> tcp_loss_callbacks_;
    std::vector<EventCallback> rtt_callbacks_;
    std::vector<EventCallback> rssi_callbacks_;
    
    struct ServerContext* server_ctx_;
    bool monitoring_active_;
    
    void invokeCallbacks(EventType type, const NetworkEvent& event);
    std::string getSignalName(EventType type) const;
};

// 全局事件管理器实例
NetworkEventManager& getEventManager();

} // namespace weaknet_dbus