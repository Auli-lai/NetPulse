// event_manager.cpp
// 事件管理器实现：处理网卡更换和上网方式改变事件

#include "event_manager.hpp"
#include "server.hpp"
#include "dbus_service.hpp"
#include "common.hpp"
#include "logger.hpp"
#include <cstdio>
#include <chrono>
#include <thread>

using namespace std::chrono_literals;

namespace weaknet_dbus {

// 全局事件管理器实例
static std::unique_ptr<NetworkEventManager> g_event_manager = std::make_unique<NetworkEventManager>();

NetworkEventManager& getEventManager() {
    return *g_event_manager;
}

NetworkEventManager::NetworkEventManager() 
    : server_ctx_(nullptr), monitoring_active_(false) {
    LOG_INFO(LogModule::EVENT_MGR, "NetworkEventManager initialized");
}

void NetworkEventManager::registerCallback(EventType type, EventCallback callback) {
    switch (type) {
        case EventType::InterfaceChanged:
            interface_callbacks_.push_back(callback);
            break;
        case EventType::ConnectionModeChanged:
            connection_mode_callbacks_.push_back(callback);
            break;
        case EventType::NetworkQualityChanged:
            network_quality_callbacks_.push_back(callback);
            break;
        case EventType::TcpLossRateChanged:
            tcp_loss_callbacks_.push_back(callback);
            break;
        case EventType::RttChanged:
            rtt_callbacks_.push_back(callback);
            break;
        case EventType::RssiChanged:
            rssi_callbacks_.push_back(callback);
            break;
    }
    LOG_INFO(LogModule::EVENT_MGR, "registered callback for event type " << static_cast<int>(type));
}

void NetworkEventManager::unregisterCallback(EventType type) {
    switch (type) {
        case EventType::InterfaceChanged:
            interface_callbacks_.clear();
            break;
        case EventType::ConnectionModeChanged:
            connection_mode_callbacks_.clear();
            break;
        case EventType::NetworkQualityChanged:
            network_quality_callbacks_.clear();
            break;
        case EventType::TcpLossRateChanged:
            tcp_loss_callbacks_.clear();
            break;
        case EventType::RttChanged:
            rtt_callbacks_.clear();
            break;
        case EventType::RssiChanged:
            rssi_callbacks_.clear();
            break;
    }
    LOG_INFO(LogModule::EVENT_MGR, "unregistered all callbacks for event type " << static_cast<int>(type));
}

std::string NetworkEventManager::getSignalName(EventType type) const {
    switch (type) {
        case EventType::InterfaceChanged:
            return kSignalInterfaceChanged;
        case EventType::ConnectionModeChanged:
            return kSignalConnectionModeChanged;
        case EventType::NetworkQualityChanged:
            return kSignalNetworkQualityChanged;
        default:
            return kSignalChanged; // 默认使用通用信号
    }
}

void NetworkEventManager::emitEvent(const NetworkEvent& event) {
    LOG_INFO(LogModule::EVENT_MGR, "emitting event: type=" << static_cast<int>(event.type) << ", message='" << event.message << "', source='" << event.source << "'");
    
    invokeCallbacks(event.type, event);
    
    // 如果有DBus服务，发送信号
    if (server_ctx_ && server_ctx_->service) {
        std::string signalName = getSignalName(event.type);
        std::string fullMessage = event.source.empty() 
            ? event.message 
            : "[" + event.source + "] " + event.message;
        
        static int32_t eventCounter = 0;
        
        // 对于网络质量事件，发送包含详细信息的信号
        if (event.type == EventType::NetworkQualityChanged && !event.details.empty()) {
            server_ctx_->service->emitNetworkQualitySignal(fullMessage, event.details, eventCounter++);
        } else {
            server_ctx_->service->emitSpecificSignal(signalName, fullMessage, eventCounter++);
        }
    }
}

void NetworkEventManager::emitInterfaceChanged(const std::string& message, const std::string& source) {
    emitEvent(NetworkEvent(EventType::InterfaceChanged, message, source, 8));
}

void NetworkEventManager::emitConnectionModeChanged(const std::string& message, const std::string& source) {
    emitEvent(NetworkEvent(EventType::ConnectionModeChanged, message, source, 9));
}

void NetworkEventManager::emitNetworkQualityChanged(const std::string& message, const std::string& details, const std::string& source) {
    NetworkEvent event(EventType::NetworkQualityChanged, message, source, 7);
    event.details = details;
    emitEvent(event);
}

void NetworkEventManager::emitTcpLossRateChanged(const std::string& message, const std::string& source) {
    emitEvent(NetworkEvent(EventType::TcpLossRateChanged, message, source, 6));
}

void NetworkEventManager::emitRttChanged(const std::string& message, const std::string& source) {
    emitEvent(NetworkEvent(EventType::RttChanged, message, source, 5));
}

void NetworkEventManager::emitRssiChanged(const std::string& message, const std::string& source) {
    emitEvent(NetworkEvent(EventType::RssiChanged, message, source, 4));
}

void NetworkEventManager::startEventMonitoring(struct ServerContext* ctx) {
    server_ctx_ = ctx;
    monitoring_active_ = true;
    
    LOG_INFO(LogModule::EVENT_MGR, "event monitoring started");
    
    // 注册一个默认回调，将事件记录到日志
    registerCallback(EventType::InterfaceChanged, [](const NetworkEvent& event) {
        LOG_INFO(LogModule::EVENT_MGR, "Interface change event: " << event.message);
    });
    
    registerCallback(EventType::ConnectionModeChanged, [](const NetworkEvent& event) {
        LOG_INFO(LogModule::EVENT_MGR, "Connection mode change event: " << event.message);
    });
}

void NetworkEventManager::stopEventMonitoring() {
    monitoring_active_ = false;
    LOG_INFO(LogModule::EVENT_MGR, "event monitoring stopped");
}

void NetworkEventManager::invokeCallbacks(EventType type, const NetworkEvent& event) {
    switch (type) {
        case EventType::InterfaceChanged:
            for (const auto& callback : interface_callbacks_) {
                callback(event);
            }
            break;
        case EventType::ConnectionModeChanged:
            for (const auto& callback : connection_mode_callbacks_) {
                callback(event);
            }
            break;
        case EventType::NetworkQualityChanged:
            for (const auto& callback : network_quality_callbacks_) {
                callback(event);
            }
            break;
        case EventType::TcpLossRateChanged:
            for (const auto& callback : tcp_loss_callbacks_) {
                callback(event);
            }
            break;
        case EventType::RttChanged:
            for (const auto& callback : rtt_callbacks_) {
                callback(event);
            }
            break;
        case EventType::RssiChanged:
            for (const auto& callback : rssi_callbacks_) {
                callback(event);
            }
            break;
    }
}

} // namespace weaknet_dbus