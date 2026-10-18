// looper.hpp
// 简单的事件循环封装，阻塞运行防止进程退出

#pragma once

#include <memory>

struct DBusConnection;

namespace weaknet_dbus {

class ServerContext;

class Looper {
public:
    static Looper* current();

    // 绑定 DBus 连接
    void attach(DBusConnection* conn);

    // 阻塞运行，直到 ctx->running 变为 false
    void run(ServerContext* ctx);

private:
    Looper() = default;
    DBusConnection* conn_ = nullptr;
};

}  // namespace weaknet_dbus


