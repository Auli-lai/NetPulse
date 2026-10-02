// ping_example.cpp
// WeakNet Ping功能使用示例

#include <iostream>
#include <string>
#include <vector>
#include <thread>
#include <chrono>
#include "weaknet_client.h"

int main() {
    std::cout << "=== WeakNet Ping功能使用示例 ===" << std::endl;

    // 初始化WeakNet客户端库
    if (!weaknet_init()) {
        std::cerr << "❌ 初始化WeakNet客户端库失败!" << std::endl;
        return 1;
    }
    std::cout << "✅ WeakNet客户端库已初始化" << std::endl;

    // 测试目标列表
    std::vector<std::string> targets = {
        "8.8.8.8",           // Google DNS
        "baidu.com",         // 百度
        "github.com",        // GitHub
        "invalidhost12345.com" // 无效主机名（用于测试错误处理）
    };

    std::cout << "\n🔍 开始Ping测试..." << std::endl;
    
    for (const auto& target : targets) {
        std::cout << "\n📡 Ping目标: " << target << std::endl;
        
        char result[512];
        char error[256];
        
        if (weaknet_ping_host(target.c_str(), result, sizeof(result), error, sizeof(error))) {
            std::cout << "   ✅ 结果: " << result << std::endl;
        } else {
            std::cout << "   ❌ 失败: " << error << std::endl;
        }
        
        // 添加小延迟避免过于频繁的ping
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }

    std::cout << "\n📊 Ping测试完成!" << std::endl;
    
    // 清理资源
    weaknet_cleanup();
    std::cout << "🧹 WeakNet客户端库已清理" << std::endl;

    return 0;
}
