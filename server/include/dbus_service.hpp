// dbus_service.hpp
// 封装 DBus 方法处理与信号发送

#pragma once

#include <string>
#include <vector>
#include <mutex>

struct DBusConnection;
struct DBusMessage;

namespace weaknet_dbus {

class ServerContext;

class DbusService {
public:
    explicit DbusService(ServerContext* ctx);
    ~DbusService() = default;

    // 注册对象路径与消息处理器
    bool register_on_connection(DBusConnection* conn);

    // 发送 Changed 信号
    bool emitChanged(const std::string& message, int32_t counter);

    // 发送特定类型的事件信号
    bool emitSpecificSignal(const std::string& signalName, const std::string& message, int32_t counter);
    
    // 发送网络质量信号（包含详细信息）
    bool emitNetworkQualitySignal(const std::string& message, const std::string& details, int32_t counter);

    // 消息分发回调在实现文件中以静态自由函数形式提供，避免在头文件依赖 dbus 类型

    // 暴露处理方法供静态分发函数调用
    bool handleGet(DBusConnection* conn, DBusMessage* msg);
    bool handleListInterfaces(DBusConnection* conn, DBusMessage* msg);
    bool handleHealthCheck(DBusConnection* conn, DBusMessage* msg);
    bool handlePing(DBusConnection* conn, DBusMessage* msg);
    bool handleGetFlows(DBusConnection* conn, DBusMessage* msg);

private:
    // 将字符串数组作为返回
    bool replyStringArray(DBusConnection* conn, DBusMessage* msg, const std::vector<std::string>& arr);

private:
    ServerContext* ctx_;
};

}  // namespace weaknet_dbus

