#pragma once

#include <string>
#include <vector>
#include <mutex>
#include <memory>

class NetInterfaceManager {
public:
    // 单例获取（线程安全懒汉，共享所有者）
    static std::shared_ptr<NetInterfaceManager> getInstance();

    // 返回当前具备上网能力的网卡名列表（UP 且存在 IPv4/IPv6 默认路由）
    std::vector<std::string> getInternetInterfaces();

    ~NetInterfaceManager() = default;

private:
    NetInterfaceManager() = default;
    NetInterfaceManager(const NetInterfaceManager&) = delete;
    NetInterfaceManager& operator=(const NetInterfaceManager&) = delete;

    static std::once_flag s_onceFlag;
    static std::shared_ptr<NetInterfaceManager> s_instance;
};
