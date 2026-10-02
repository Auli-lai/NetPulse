// logger.hpp
// WeakNet 统一日志系统 - 基于 Google glog

#pragma once

#include <glog/logging.h>
#include <string>
#include <memory>

namespace weaknet_dbus {

// 日志级别枚举
enum class LogLevel {
    INFO = 0,
    WARNING = 1,
    ERROR = 2,
    FATAL = 3
};

// 日志模块标识
namespace LogModule {
    constexpr const char* SERVER = "server";
    constexpr const char* CLIENT = "client";
    constexpr const char* DBUS = "dbus";
    constexpr const char* WEAK_MGR = "weak_mgr";
    constexpr const char* TCP_LOSS = "tcp_loss";
    constexpr const char* RTT = "rtt";
    constexpr const char* RSSI = "rssi";
    constexpr const char* NETWORK = "network";
    constexpr const char* EVENT_MGR = "event_mgr";
    constexpr const char* PING = "ping";
    constexpr const char* INTERFACE = "interface";
}

// 日志初始化类
class Logger {
public:
    // 初始化日志系统
    static bool init(const std::string& program_name, 
                    const std::string& log_dir = "./logs/server",
                    LogLevel min_level = LogLevel::INFO,
                    bool log_to_stderr = true);
    
    // 清理日志系统
    static void shutdown();
    
    // 设置日志级别
    static void setLogLevel(LogLevel level);
    
    // 设置日志目录
    static void setLogDir(const std::string& dir);
    
    // 检查是否已初始化
    static bool isInitialized() { return initialized_; }

private:
    static bool initialized_;
    static std::string current_log_dir_;
};

// 便捷的日志宏定义
#define LOG_INFO(module, msg) LOG(INFO) << "[" << module << "] " << msg
#define LOG_WARNING(module, msg) LOG(WARNING) << "[" << module << "] " << msg
#define LOG_ERROR(module, msg) LOG(ERROR) << "[" << module << "] " << msg
#define LOG_FATAL(module, msg) LOG(FATAL) << "[" << module << "] " << msg

// 带格式的日志宏
#define LOG_INFO_F(module, fmt, ...) \
    LOG(INFO) << "[" << module << "] " << std::string().append(fmt).c_str() << __VA_ARGS__

#define LOG_ERROR_F(module, fmt, ...) \
    LOG(ERROR) << "[" << module << "] " << std::string().append(fmt).c_str() << __VA_ARGS__

// 条件日志宏
#define LOG_IF_INFO(condition, module, msg) \
    LOG_IF(INFO, condition) << "[" << module << "] " << msg

#define LOG_IF_ERROR(condition, module, msg) \
    LOG_IF(ERROR, condition) << "[" << module << "] " << msg

// 调试日志宏 (仅在DEBUG模式下编译)
#ifdef DEBUG
#define LOG_DEBUG(module, msg) LOG(INFO) << "[DEBUG][" << module << "] " << msg
#else
#define LOG_DEBUG(module, msg) do {} while(0)
#endif

// 性能日志宏
#define LOG_PERF(module, operation, duration_ms) \
    LOG(INFO) << "[" << module << "] PERF: " << operation << " took " << duration_ms << "ms"

// 网络状态日志宏
#define LOG_NETWORK_STATE(module, interface, state) \
    LOG(INFO) << "[" << module << "] Network interface " << interface << " state: " << state

// 事件日志宏
#define LOG_EVENT(module, event_type, message) \
    LOG(INFO) << "[" << module << "] EVENT: " << event_type << " - " << message

// 错误处理日志宏
#define LOG_ERROR_WITH_CODE(module, operation, error_code) \
    LOG(ERROR) << "[" << module << "] " << operation << " failed with error code: " << error_code

} // namespace weaknet_dbus


