// logger.cpp
// WeakNet 统一日志系统实现

#include "logger.hpp"
#include <iostream>
#include <filesystem>

namespace weaknet_dbus {

// 静态成员初始化
bool Logger::initialized_ = false;
std::string Logger::current_log_dir_ = "";

bool Logger::init(const std::string& program_name, 
                 const std::string& log_dir, 
                 LogLevel min_level,
                 bool log_to_stderr) {
    if (initialized_) {
        return true;
    }

    try {
        // 创建日志目录
        std::filesystem::create_directories(log_dir);
        
        // 设置日志目录
        FLAGS_log_dir = log_dir;
        FLAGS_max_log_size = 10; // 10MB
        FLAGS_minloglevel = static_cast<int>(min_level);
        FLAGS_logtostderr = log_to_stderr;
        FLAGS_alsologtostderr = true;
        FLAGS_colorlogtostderr = true;
        
        // 初始化glog
        try {
            google::InitGoogleLogging(program_name.c_str());
        } catch (const std::exception& e) {
            std::cerr << "[logger] Failed to initialize glog: " << e.what() << std::endl;
            return false;
        } catch (...) {
            std::cerr << "[logger] Failed to initialize glog: unknown error" << std::endl;
            return false;
        }
        
        current_log_dir_ = log_dir;
        initialized_ = true;
        
        LOG(INFO) << "[logger] Logger initialized successfully";
        LOG(INFO) << "[logger] Log directory: " << log_dir;
        LOG(INFO) << "[logger] Min log level: " << static_cast<int>(min_level);
        LOG(INFO) << "[logger] Log to stderr: " << (log_to_stderr ? "true" : "false");
        
        return true;
    } catch (const std::exception& e) {
        std::cerr << "[logger] Failed to initialize logger: " << e.what() << std::endl;
        return false;
    }
}

void Logger::shutdown() {
    if (initialized_) {
        LOG(INFO) << "[logger] Shutting down logger";
        google::ShutdownGoogleLogging();
        initialized_ = false;
    }
}

void Logger::setLogLevel(LogLevel level) {
    if (initialized_) {
        FLAGS_minloglevel = static_cast<int>(level);
        LOG(INFO) << "[logger] Log level changed to: " << static_cast<int>(level);
    }
}

void Logger::setLogDir(const std::string& dir) {
    if (initialized_) {
        try {
            std::filesystem::create_directories(dir);
            FLAGS_log_dir = dir;
            current_log_dir_ = dir;
            LOG(INFO) << "[logger] Log directory changed to: " << dir;
        } catch (const std::exception& e) {
            LOG(ERROR) << "[logger] Failed to change log directory: " << e.what();
        }
    }
}

} // namespace weaknet_dbus
