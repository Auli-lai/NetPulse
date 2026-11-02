// test_client.cpp
// WeakNet 客户端动态库完整接口验证工具
// 验证client.cpp中提供的所有API接口和事件接口

#include <cstdio>
#include <cstdlib>
#include <string>
#include <unistd.h>
#include <vector>
#include <chrono>
#include <thread>

#include "weaknet_client.h"
#include "logger.hpp"

// 测试结果统计
struct TestStats {
    int total = 0;
    int passed = 0;
    int failed = 0;
    
    void addResult(bool success) {
        total++;
        if (success) passed++;
        else failed++;
    }
    
    void print() {
        printf("\n📊 测试统计: 总计=%d, 通过=%d, 失败=%d, 成功率=%.1f%%\n", 
               total, passed, failed, total > 0 ? (passed * 100.0 / total) : 0.0);
    }
};

// 全局测试统计
TestStats g_stats;

// 测试辅助宏
#define TEST_CASE(name) \
    printf("\n🧪 测试: %s\n", name); \
    g_stats.addResult(true)

#define TEST_ASSERT(condition, message) \
    do { \
        if (!(condition)) { \
            printf("   ❌ 失败: %s\n", message); \
            g_stats.failed++; \
            g_stats.passed--; \
            return false; \
        } else { \
            printf("   ✅ 通过: %s\n", message); \
        } \
    } while(0)

#define TEST_API_CALL(func, ...) \
    do { \
        if (func(__VA_ARGS__)) { \
            printf("   ✅ API调用成功\n"); \
        } else { \
            printf("   ❌ API调用失败\n"); \
            return false; \
        } \
    } while(0)

// 测试基础功能
bool testBasicFunctions() {
    TEST_CASE("基础功能测试");

    char buffer[512];

    // 测试初始化
    TEST_ASSERT(weaknet_init(), "库初始化");
    
    // 测试连接状态
    TEST_ASSERT(weaknet_is_connected(), "连接状态检查");
    
    // 测试版本信息
    TEST_ASSERT(weaknet_get_version(buffer, sizeof(buffer)), "获取版本信息");
    printf("   📦 版本: %s\n", buffer);
    
    // 测试编译信息
    TEST_ASSERT(weaknet_get_build_info(buffer, sizeof(buffer)), "获取编译信息");
    printf("   🔧 编译信息: %s\n", buffer);
    
    return true;
}

// 测试网络接口信息获取
bool testNetworkInfo() {
    TEST_CASE("网络接口信息获取");
    
    char buffer[4096], error[256];
    
    // 测试获取网络接口
    TEST_API_CALL(weaknet_get_interfaces, buffer, sizeof(buffer), error, sizeof(error));
    printf("   📡 网络接口: %s\n", buffer);
    
    // 测试健康检查
    TEST_API_CALL(weaknet_health_check, buffer, sizeof(buffer), error, sizeof(error));
    printf("   💚 健康检查: %s\n", buffer);
    
    // 测试从文件读取
    if (weaknet_get_from_file(buffer, sizeof(buffer), error, sizeof(error))) {
        printf("   📄 文件内容: %s\n", buffer);
    } else {
        printf("   ℹ️  文件读取: %s\n", error);
    }
    
    return true;
}

// 测试Ping功能
bool testPingFunction() {
    TEST_CASE("Ping功能测试");
    
    char result[512], error[256];
    std::vector<std::string> targets = {
        "8.8.8.8",           // Google DNS
        "baidu.com",         // 百度
        "127.0.0.1",         // 本地回环
        "invalidhost12345.com" // 无效主机名
    };
    
    for (const auto& target : targets) {
        printf("   🎯 Ping目标: %s\n", target.c_str());
        
        if (weaknet_ping_host(target.c_str(), result, sizeof(result), error, sizeof(error))) {
            printf("     ✅ 结果: %s\n", result);
        } else {
            printf("     ❌ 失败: %s\n", error);
        }
        
        // 添加延迟避免过于频繁
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }
    
    return true;
}

// 测试事件系统
bool testEventSystem() {
    TEST_CASE("事件系统测试");
    
    char buffer[512], error[256];
    
    // 测试获取事件类型
    TEST_API_CALL(weaknet_get_event_types, buffer, sizeof(buffer), error, sizeof(error));
    printf("   📋 支持的事件类型: %s\n", buffer);
    
    // 测试订阅事件
    std::vector<std::string> eventTypes = {
        "InterfaceChanged",
        "ConnectionModeChanged"
    };
    
    for (const auto& eventType : eventTypes) {
        printf("   🔔 订阅事件: %s\n", eventType.c_str());
        TEST_API_CALL(weaknet_subscribe_event, eventType.c_str(), nullptr);
    }
    
    // 测试事件检查
    printf("   🔍 检查事件 (5秒)...\n");
    char eventType[64], message[512], source[64];
    int32_t counter;
    
    for (int i = 0; i < 5; i++) {
        if (weaknet_check_events(eventType, sizeof(eventType), message, sizeof(message),
                                 &counter, source, sizeof(source), error, sizeof(error))) {
            printf("     🎯 检测到事件: type=%s counter=%d source=%s message=%s\n", 
                   eventType, counter, source, message);
        } else {
            printf("     ⏳ 第%d秒: 无事件\n", i+1);
        }
        sleep(1);
    }
    
    // 测试取消订阅
    for (const auto& eventType : eventTypes) {
        printf("   🔕 取消订阅: %s\n", eventType.c_str());
        TEST_API_CALL(weaknet_unsubscribe_event, eventType.c_str());
    }
    
    return true;
}

// 测试网络质量事件监听
bool testNetworkQualityEvents() {
    TEST_CASE("网络质量事件监听测试");
    
    char quality[256], details[1024], error[256];
    int32_t counter;
    
    // 测试非阻塞检查网络质量事件
    printf("   🔍 检查网络质量事件 (10秒)...\n");
    for (int i = 0; i < 10; i++) {
        if (weaknet_check_network_quality(quality, sizeof(quality), details, sizeof(details),
                                         &counter, error, sizeof(error))) {
            printf("     🎯 检测到网络质量事件:\n");
            printf("       质量等级: %s\n", quality);
            printf("       详细信息: %s\n", details);
            printf("       事件计数: %d\n", counter);
        } else {
            printf("     ⏳ 第%d秒: 无网络质量事件 (%s)\n", i+1, error);
        }
        sleep(1);
    }
    
    return true;
}

// 测试网络质量事件回调
bool testNetworkQualityCallback() {
    TEST_CASE("网络质量事件回调测试");
    
    // 定义回调函数
    static int callback_count = 0;
    auto quality_callback = [](const char* quality, const char* details, int32_t counter) -> bool {
        callback_count++;
        printf("     🎯 回调收到网络质量事件 #%d:\n", callback_count);
        printf("       质量等级: %s\n", quality);
        printf("       详细信息: %s\n", details);
        printf("       事件计数: %d\n", counter);
        
        // 收到3个事件后停止监听
        return callback_count < 3;
    };
    
    printf("   🔔 订阅网络质量事件回调 (最多3个事件)...\n");
    if (weaknet_subscribe_network_quality(quality_callback)) {
        printf("     ✅ 网络质量事件订阅成功\n");
    } else {
        printf("     ❌ 网络质量事件订阅失败\n");
        return false;
    }
    
    return true;
}

// 测试状态变化监控
bool testChangeMonitoring() {
    TEST_CASE("状态变化监控");
    
    char message[512], error[256];
    int32_t counter;
    
    printf("   🔍 检查状态变化 (3秒)...\n");
    for (int i = 0; i < 3; i++) {
        if (weaknet_check_changes(message, sizeof(message), &counter, error, sizeof(error))) {
            printf("     🎯 检测到变化: %s (counter=%d)\n", message, counter);
        } else {
            printf("     ⏳ 第%d秒: 无变化\n", i+1);
        }
        sleep(1);
    }
    
    return true;
}

// 测试错误处理
bool testErrorHandling() {
    TEST_CASE("错误处理测试");
    
    char buffer[512], error[256];
    
    // 测试空主机名ping
    printf("   🚫 测试空主机名ping\n");
    if (!weaknet_ping_host("", buffer, sizeof(buffer), error, sizeof(error))) {
        printf("     ✅ 正确处理空主机名: %s\n", error);
    } else {
        printf("     ❌ 应该拒绝空主机名\n");
        return false;
    }
    
    // 测试null主机名ping
    printf("   🚫 测试null主机名ping\n");
    if (!weaknet_ping_host(nullptr, buffer, sizeof(buffer), error, sizeof(error))) {
        printf("     ✅ 正确处理null主机名: %s\n", error);
    } else {
        printf("     ❌ 应该拒绝null主机名\n");
        return false;
    }
    
    return true;
}

// 性能测试
bool testPerformance() {
    TEST_CASE("性能测试");
    
    char buffer[512], error[256];
    int testCount = 10;
    
    printf("   ⚡ 执行%d次get_interfaces调用\n", testCount);
    auto start = std::chrono::high_resolution_clock::now();
    
    for (int i = 0; i < testCount; i++) {
        if (!weaknet_get_interfaces(buffer, sizeof(buffer), error, sizeof(error))) {
            printf("     ❌ 第%d次调用失败: %s\n", i+1, error);
            return false;
        }
    }
    
    auto end = std::chrono::high_resolution_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::milliseconds>(end - start);
    
    printf("     ✅ 完成%d次调用，耗时%ldms，平均%.2fms/次\n", 
           testCount, duration.count(), duration.count() / (double)testCount);
    
    return true;
}

// 运行所有测试
void runAllTests() {
    printf("🚀 开始WeakNet客户端库完整接口验证\n");
    printf("============================================================\n");
    
    // 初始化
    if (!weaknet_init()) {
        printf("❌ 初始化失败，无法继续测试\n");
        return;
    }
    
    // 运行各项测试
    testBasicFunctions();
    testNetworkInfo();
    testPingFunction();
    testEventSystem();
    testNetworkQualityEvents();
    testNetworkQualityCallback();
    testChangeMonitoring();
    testErrorHandling();
    testPerformance();
    
    // 清理
    weaknet_cleanup();
    
    // 打印统计
    g_stats.print();
    
    printf("\n🎉 WeakNet客户端库接口验证完成!\n");
}

// 单个命令测试
bool runSingleTest(const std::string& command, int argc, char* argv[]) {
    if (!weaknet_init()) {
        printf("❌ 初始化失败\n");
        return false;
    }
    
    char buffer[4096], error[256];
    int32_t counter;
    
    if (command == "get") {
        if (weaknet_get_interfaces(buffer, sizeof(buffer), error, sizeof(error))) {
            printf("✅ 网络接口信息: %s\n", buffer);
        } else {
            printf("❌ 获取失败: %s\n", error);
            return false;
        }
    }
    else if (command == "health") {
        if (weaknet_health_check(buffer, sizeof(buffer), error, sizeof(error))) {
            printf("✅ 健康检查结果: %s\n", buffer);
        } else {
            printf("❌ 健康检查失败: %s\n", error);
            return false;
        }
    }
    else if (command == "file") {
        if (weaknet_get_from_file(buffer, sizeof(buffer), error, sizeof(error))) {
            printf("✅ 文件中的状态: %s\n", buffer);
        } else {
            printf("❌ 读取文件失败: %s\n", error);
            return false;
        }
    }
    else if (command == "ping") {
        if (argc < 3) {
            printf("❌ Ping命令需要指定主机名\n");
            printf("用法: %s ping HOSTNAME\n", argv[0]);
            return false;
        }
        std::string hostname = argv[2];
        if (weaknet_ping_host(hostname.c_str(), buffer, sizeof(buffer), error, sizeof(error))) {
            printf("✅ Ping结果: %s\n", buffer);
        } else {
            printf("❌ Ping失败: %s\n", error);
            return false;
        }
    }
    else if (command == "check") {
        if (weaknet_check_changes(buffer, sizeof(buffer), &counter, error, sizeof(error))) {
            printf("✅ 检测到变化: %s (counter=%d)\n", buffer, counter);
        } else {
            printf("ℹ️  没有检测到新变化: %s\n", error);
        }
    }
    else if (command == "events") {
        char eventType[64], message[512], source[64];
        if (weaknet_check_events(eventType, sizeof(eventType), message, sizeof(message),
                                  &counter, source, sizeof(source), error, sizeof(error))) {
            printf("✅ 检测到事件: type=%s counter=%d source=%s message=%s\n", 
                        eventType, counter, source, message);
        } else {
            printf("ℹ️  没有检测到事件: %s\n", error);
        }
    }
    else if (command == "quality") {
        char quality[256], details[1024];
        if (weaknet_check_network_quality(quality, sizeof(quality), details, sizeof(details),
                                         &counter, error, sizeof(error))) {
            printf("✅ 检测到网络质量事件:\n");
            printf("   质量等级: %s\n", quality);
            printf("   详细信息: %s\n", details);
            printf("   事件计数: %d\n", counter);
        } else {
            printf("ℹ️  没有检测到网络质量事件: %s\n", error);
        }
    }
    else if (command == "quality-sub") {
        printf("🔄 开始监听网络质量事件（按Ctrl+C停止）...\n");
        char quality[256], details[1024];
        int count = 0;
        while (count < 60) { // 最多运行1分钟
            if (weaknet_check_network_quality(quality, sizeof(quality), details, sizeof(details),
                                             &counter, error, sizeof(error))) {
                printf("📊 网络质量事件: %s (分数: %s, 计数: %d)\n", quality, details, counter);
            }
            usleep(1000000); // 等待1秒
            count++;
        }
    }
    else if (command == "event-types") {
        if (weaknet_get_event_types(buffer, sizeof(buffer), error, sizeof(error))) {
            printf("✅ 支持的事件类型: %s\n", buffer);
        } else {
            printf("❌ 获取事件类型失败: %s\n", error);
            return false;
        }
    }
    else if (command == "event-sub" && argc >= 3) {
        std::string eventType = argv[2];
        if (weaknet_subscribe_event(eventType.c_str(), nullptr)) {
            printf("✅ 成功订阅事件类型: %s\n", eventType.c_str());
        } else {
            printf("❌ 订阅事件失败: %s\n", eventType.c_str());
            return false;
        }
    }
    else if (command == "subscribe") {
        printf("🔄 开始持续监听网络变化（按Ctrl+C停止）...\n");
        int count = 0;
        while (count < 300) { // 最多运行5分钟
            if (weaknet_check_changes(buffer, sizeof(buffer), &counter, error, sizeof(error))) {
                printf("📢 收到网络变化: %s (counter=%d)\n", buffer, counter);
            }
            usleep(1000000); // 等待1秒
            count++;
        }
    }
    else if (command == "test-basic") {
        return testBasicFunctions();
    }
    else if (command == "test-network") {
        return testNetworkInfo();
    }
    else if (command == "test-ping") {
        return testPingFunction();
    }
    else if (command == "test-events") {
        return testEventSystem();
    }
    else if (command == "test-errors") {
        return testErrorHandling();
    }
    else if (command == "test-performance") {
        return testPerformance();
    }
    else if (command == "test-quality") {
        return testNetworkQualityEvents();
    }
    else if (command == "test-quality-callback") {
        return testNetworkQualityCallback();
    }
    else {
        printf("❌ 未知命令: %s\n", command.c_str());
        return false;
    }
    
    weaknet_cleanup();
    return true;
}

int main(int argc, char* argv[]) {
    if (argc < 2) {
        printf("WeakNet 客户端动态库完整接口验证工具\n");
        printf("用法:\n");
        printf("  %s all                    - 运行所有接口验证测试\n", argv[0]);
        printf("  %s get                    - 获取当前网络接口\n", argv[0]);
        printf("  %s health                 - 网络健康检查\n", argv[0]);
        printf("  %s file                   - 从文件读取最新状态\n", argv[0]);
        printf("  %s ping HOSTNAME          - Ping指定主机\n", argv[0]);
        printf("  %s check                  - 单次检查变化\n", argv[0]);
        printf("  %s subscribe              - 持续监听网络变化\n", argv[0]);
        printf("  %s events                 - 单次检查事件\n", argv[0]);
        printf("  %s quality                - 单次检查网络质量事件\n", argv[0]);
        printf("  %s quality-sub            - 持续监听网络质量事件\n", argv[0]);
        printf("  %s event-types            - 获取支持的事件类型\n", argv[0]);
        printf("  %s event-sub EVENT_TYPE   - 订阅特定事件类型\n", argv[0]);
        printf("\n测试模式:\n");
        printf("  %s test-basic             - 基础功能测试\n", argv[0]);
        printf("  %s test-network           - 网络信息测试\n", argv[0]);
        printf("  %s test-ping              - Ping功能测试\n", argv[0]);
        printf("  %s test-events            - 事件系统测试\n", argv[0]);
        printf("  %s test-quality           - 网络质量事件测试\n", argv[0]);
        printf("  %s test-quality-callback  - 网络质量事件回调测试\n", argv[0]);
        printf("  %s test-errors            - 错误处理测试\n", argv[0]);
        printf("  %s test-performance       - 性能测试\n", argv[0]);
        return 1;
    }

    std::string command = argv[1];
    
    if (command == "all") {
        runAllTests();
    } else {
        runSingleTest(command, argc, argv);
    }

    return 0;
}
