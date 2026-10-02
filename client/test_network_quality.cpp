// test_network_quality.cpp
// 测试网络质量事件监听功能

#include <iostream>
#include <cstring>
#include <unistd.h>
#include <signal.h>
#include "weaknet_client.h"

// 全局变量用于信号处理
static bool running = true;

// 信号处理函数
void signal_handler(int sig) {
    if (sig == SIGINT || sig == SIGTERM) {
        std::cout << "\n收到退出信号，正在停止..." << std::endl;
        running = false;
    }
}

// 网络质量事件回调函数
bool network_quality_callback(const char* quality, const char* details, int32_t counter) {
    std::cout << "\n=== 网络质量事件 ===" << std::endl;
    std::cout << "质量等级: " << quality << std::endl;
    std::cout << "事件计数: " << counter << std::endl;
    std::cout << "详细信息: " << details << std::endl;
    std::cout << "===================" << std::endl;
    
    // 返回true继续监听，返回false停止监听
    return running;
}

int main() {
    std::cout << "WeakNet 网络质量事件监听测试程序" << std::endl;
    std::cout << "按 Ctrl+C 退出程序" << std::endl;
    
    // 设置信号处理
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    
    // 初始化客户端
    if (!weaknet_init()) {
        std::cerr << "错误: 无法初始化WeakNet客户端" << std::endl;
        return 1;
    }
    
    std::cout << "客户端初始化成功" << std::endl;
    
    // 等待连接建立
    int retry_count = 0;
    while (!weaknet_is_connected() && retry_count < 10) {
        std::cout << "等待连接到服务器..." << std::endl;
        sleep(1);
        retry_count++;
    }
    
    if (!weaknet_is_connected()) {
        std::cerr << "错误: 无法连接到WeakNet服务器" << std::endl;
        weaknet_cleanup();
        return 1;
    }
    
    std::cout << "已连接到服务器，开始监听网络质量事件..." << std::endl;
    
    // 订阅网络质量事件
    if (!weaknet_subscribe_network_quality(network_quality_callback)) {
        std::cerr << "错误: 无法订阅网络质量事件" << std::endl;
        weaknet_cleanup();
        return 1;
    }
    
    std::cout << "网络质量事件订阅成功，开始监听..." << std::endl;
    
    // 主循环 - 非阻塞检查事件
    while (running) {
        char quality[256] = {0};
        char details[1024] = {0};
        char error[256] = {0};
        int32_t counter = 0;
        
        if (weaknet_check_network_quality(quality, sizeof(quality), 
                                        details, sizeof(details), 
                                        &counter, error, sizeof(error))) {
            std::cout << "\n=== 检测到网络质量事件 ===" << std::endl;
            std::cout << "质量等级: " << quality << std::endl;
            std::cout << "事件计数: " << counter << std::endl;
            std::cout << "详细信息: " << details << std::endl;
            std::cout << "=========================" << std::endl;
        }
        
        // 短暂休眠避免CPU占用过高
        usleep(100000); // 100ms
    }
    
    std::cout << "正在清理资源..." << std::endl;
    weaknet_cleanup();
    
    std::cout << "程序已退出" << std::endl;
    return 0;
}
