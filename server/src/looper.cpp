// looper.cpp
// 实现阻塞式 looper

#include <dbus/dbus.h>

#include "server.hpp"
#include "looper.hpp"

namespace weaknet_dbus {

static thread_local Looper* t_looper = nullptr;

Looper* Looper::current() {
    if (!t_looper) t_looper = new Looper();
    return t_looper;
}

void Looper::attach(DBusConnection* conn) {
    conn_ = conn;
}

void Looper::run(ServerContext* ctx) {
    // -1 代表阻塞等待直到有事件或超时
    while (ctx->running.load()) {
        dbus_connection_read_write_dispatch(conn_, -1);
    }
}

}  // namespace weaknet_dbus


