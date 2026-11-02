#pragma once

#include <string>
#include <memory>
#include <mutex>
#include <cstdint>

// 方法标志位：当前上网方式
// 0x1: 存在 IPv4 默认路由
// 0x2: 存在 IPv6 默认路由
namespace UsingMethodFlag {
    static constexpr uint32_t IPv4Default = 0x1;
    static constexpr uint32_t IPv6Default = 0x2;
}

class UsingInterfaceManager {
public:
    // 线程安全懒汉单例
    static std::shared_ptr<UsingInterfaceManager> getInstance();

    // 启动后台监听（可重复调用，幂等）
    void start();

    // 获取当前用于上网的网卡名（依据默认路由与接口UP状态决定），无则返回空字符串
    std::string getCurrentInterface();

    // 返回方法标志位，参考 UsingMethodFlag
    uint32_t getMethodFlags();

    ~UsingInterfaceManager();

private:
    UsingInterfaceManager();
    UsingInterfaceManager(const UsingInterfaceManager&) = delete;
    UsingInterfaceManager& operator=(const UsingInterfaceManager&) = delete;

    static std::once_flag s_onceFlag;
    static std::shared_ptr<UsingInterfaceManager> s_instance;

    // 内部状态保护
    std::mutex stateMutex_;
    std::string currentIfName_;
    uint32_t methodFlags_ = 0;

    // 平台相关实现细节隐藏在 .cpp
    struct Impl;
    Impl* impl_ = nullptr;
};
