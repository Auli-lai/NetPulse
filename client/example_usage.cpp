// example_usage.cpp
// WeakNet 动态库使用示例
// 演示如何在其他C++程序中集成WeakNet客户端库

#include <iostream>
#include <thread>
#include <chrono>
#include "weaknet_client.h"

void onNetworkChange(const char* event_type, const char* message, int32_t counter, const char* source) {
    std::cout << "🔄 收到事件: type=" << event_type 
              << " counter=" << counter 
              << " source=" << source 
              << " message=" << message << std::endl;
}

int main() {
    std::cout << "=== WeakNet 动态库使用示例 ===" << std::endl;

    // 1. 初始化WeakNet客户端库
    std::cout << "\n1. 初始化WeakNet客户端库..." << std::endl;
    if (!weaknet_init()) {
        std::cerr << "❌ 初始化失败!" << std::endl;
        return 1;
    }
    std::cout << "✅ 初始化成功!" << std::endl;

    // 2. 检查连接状态
    std::cout << "\n2. 检查连接状态..." << std::endl;
    if (weaknet_is_connected()) {
        std::cout << "✅ 已连接到WeakNet服务" << std::endl;
    } else {
        std::cout << "❌ 未连接到WeakNet服务" << std::endl;
    }

    // 3. 获取库版本信息
    char version_info[256];
    if (weaknet_get_version(version_info, sizeof(version_info))) {
        std::cout << "📦 库版本: " << version_info << std::endl;
    }

    char build_info[256];
    if (weaknet_get_build_info(build_info, sizeof(build_info))) {
        std::cout << "🔧 编译信息: " << build_info << std::endl;
    }

    // 4. 获取网络接口信息
    std::cout << "\n3. 获取网络接口信息..." << std::endl;
    char interface_info[4096];
    char error_buffer[256];
    
    if (weaknet_get_interfaces(interface_info, sizeof(interface_info), error_buffer, sizeof(error_buffer))) {
        std::cout << "✅ 网络接口信息: " << interface_info << std::endl;
    } else {
        std::cout << "❌ 获取失败: " << error_buffer << std::endl;
    }

    // 5. 网络健康检查
    std::cout << "\n4. 执行网络健康检查..." << std::endl;
    char health_result[4096];
    
    if (weaknet_health_check(health_result, sizeof(health_result), error_buffer, sizeof(error_buffer))) {
        std::cout << "✅ 健康检查结果: " << health_result << std::endl;
    } else {
        std::cout << "❌ 健康检查失败: " << error_buffer << std::endl;
    }

    // 6. 获取支持的事件类型
    std::cout << "\n5. 获取支持的事件类型..." << std::endl;
    char event_types[256];
    
    if (weaknet_get_event_types(event_types, sizeof(event_types), error_buffer, sizeof(error_buffer))) {
        std::cout << "✅ 支持的事件类型: " << event_types << std::endl;
    } else {
        std::cout << "❌ 获取事件类型失败: " << error_buffer << std::endl;
    }

    // 7. 订阅网络变化事件
    std::cout << "\n6. 订阅网络变化事件..." << std::endl;
    
    if (weaknet_subscribe_event("InterfaceChanged", onNetworkChange)) {
        std::cout << "✅ 已订阅InterfaceChanged事件" << std::endl;
    } else {
        std::cout << "❌ 订阅InterfaceChanged事件失败" << std::endl;
    }

    if (weaknet_subscribe_event("ConnectionModeChanged", onNetworkChange)) {
        std::cout << "✅ 已订阅ConnectionModeChanged事件" << std::endl;
    } else {
        std::cout << "❌ 订阅ConnectionModeChanged事件失败" << std::endl;
    }

    // 8. 监控事件（持续30秒）
    std::cout << "\n7. 开始事件监控（30秒）..." << std::endl;
    std::cout << "   注意: 在新终端中运行服务器或改变网络状态来触发事件" << std::endl;
    
    char event_type_buffer[64];
    char message_buffer[512];
    char source_buffer[64];
    int32_t counter;
    
    auto start_time = std::chrono::steady_clock::now();
    auto end_time = start_time + std::chrono::seconds(30);
    
    int event_count = 0;
    
    while (std::chrono::steady_-clock::now() < end_time) {
        auto now = std::chrono::steady_clock::now();
        auto remaining = std::chrono::duration_cast<std::chrono::seconds>(end_time - now).count();
        
        if (weaknet_check_events(event_type_buffer, sizeof(event_type_buffer),
                                  message_buffer, sizeof(message_buffer),
                                  &counter, source_buffer, sizeof(source_buffer),
                                  error_buffer, sizeof(error_buffer))) {
            event_count++;
            std::cout << "\n🎯 EVENT #" << event_count << ":" << std::endl;
            std::cout << "   Type: " << event_type_buffer << std::endl;
            std::cout << "   Counter: " << counter << std::endl;
            std::cout << "   Source: " << source_buffer << std::endl;
            std::cout << "   Message: " << message_buffer << std::endl;
        }
        
        std::cout << "\r⏳ 剩余时间: " << remaining << "秒 (检测到 " << event_count << " 个事件)";
        std::cout.flush();
        
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    
    std::cout << std::endl;
    
    // 9. 取消订阅
    std::cout << "\n8. 取消事件订阅..." << std::endl;
    
    if (weaknet_unsubscribe_event("InterfaceChanged")) {
        std::cout << "✅ 已取消InterfaceChanged事件订阅" << std::endl;
    }
    
    if (weaknet_unsubscribe_event("ConnectionModeChanged")) {
        std::cout << "✅ 已取消ConnectionModeChanged事件订阅" << std::endl;
    }

    // 10. 清理资源
    std::cout << "\n9. 清理资源..." << std::endl;
    weaknet_cleanup();
    std::cout << "✅ WeakNet客户端库已清理" << std::endl;

    // 总结
    std::cout << "\n=== 使用示例完成 ===" << std::endl;
    std::cout << "📊 本次运行检测到 " << event_count << " 个事件" << std::endl;
    std::cout << "💡 提示: 要看到实际事件，请:" << std::endl;
    std::cout << "    - 确保WeakNet服务器正在运行" << std::endl;
    std::cout << "    - 插入/拔出网络设备" << std::endl;
    std::cout << "    - 切换网络连接" << std::endl;
    std::cout << "    - 改变网络配置" << std::endl;

    return 0;
}