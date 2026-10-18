// main.cpp
// 作为服务端可执行程序的入口，调用 start_server

#include "server.hpp"

int main() {
    return weaknet_dbus::start_server();
}
