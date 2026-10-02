// dbus_service.cpp
// 实现 DBus 服务类：方法处理与信号发送

#include <dbus/dbus.h>
#include <cstdio>
#include <cstring>
#include <sstream>
#include "logger.hpp"

#include "common.hpp"
#include "serializer.hpp"
#include "server.hpp"
#include "dbus_service.hpp"
#include "weak_netmgr.hpp"
#include "net_info.hpp"
#include "network_quality_assessor.hpp"
#include "net_ping.h"
#include "net_traffic.h"   // NetTrafficAnalyzer / FlowRate —— GetFlows 用

namespace weaknet_dbus {

DbusService::DbusService(ServerContext* ctx) : ctx_(ctx) {}

// 静态自由函数，转调到对象实例
static DBusHandlerResult MessageHandlerStatic(DBusConnection* conn, DBusMessage* msg, void* user_data) {
    auto* self = reinterpret_cast<DbusService*>(user_data);
    if (!self) return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
    if (dbus_message_is_method_call(msg, kInterface, kMethodGet)) {
        self->handleGet(conn, msg);
        return DBUS_HANDLER_RESULT_HANDLED;
    }
    if (dbus_message_is_method_call(msg, kInterface, kMethodListInterfaces)) {
        self->handleListInterfaces(conn, msg);
        return DBUS_HANDLER_RESULT_HANDLED;
    }
    if (dbus_message_is_method_call(msg, kInterface, kMethodGetInterfaces)) {
        self->handleListInterfaces(conn, msg);
        return DBUS_HANDLER_RESULT_HANDLED;
    }
    if (dbus_message_is_method_call(msg, kInterface, kMethodHealthCheck)) {
        self->handleHealthCheck(conn, msg);
        return DBUS_HANDLER_RESULT_HANDLED;
    }
    if (dbus_message_is_method_call(msg, kInterface, kMethodPing)) {
        self->handlePing(conn, msg);
        return DBUS_HANDLER_RESULT_HANDLED;
    }
    if (dbus_message_is_method_call(msg, kInterface, kMethodGetFlows)) {
        self->handleGetFlows(conn, msg);
        return DBUS_HANDLER_RESULT_HANDLED;
    }
    return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
}

bool DbusService::register_on_connection(DBusConnection* conn) {
    static DBusObjectPathVTable vtable{};
    vtable.message_function = &MessageHandlerStatic;
    return dbus_connection_register_object_path(conn, kObjectPath, &vtable, this);
}

bool DbusService::emitChanged(const std::string& message, int32_t counter) {
    DBusMessage* sig = dbus_message_new_signal(kObjectPath, kInterface, kSignalChanged);
    if (!sig) return false;
    DBusMessageIter args;
    dbus_message_iter_init_append(sig, &args);
    const char* s = message.c_str();
    if (!dbus_message_iter_append_basic(&args, DBUS_TYPE_STRING, &s)) { dbus_message_unref(sig); return false; }
    if (!dbus_message_iter_append_basic(&args, DBUS_TYPE_INT32, &counter)) { dbus_message_unref(sig); return false; }
    bool ok = dbus_connection_send(ctx_->connection, sig, nullptr);
    dbus_connection_flush(ctx_->connection);
    dbus_message_unref(sig);
    ChangedPayload payload{message, counter};
    std::string err;
    serializeChangedPayloadToFile(payload, kSignalSerializedFile, &err);
    return ok;
}

// MessageHandler 实现已移动到静态自由函数

bool DbusService::handleGet(DBusConnection* conn, DBusMessage* msg) {
    const char* reply_text = "Hello from WeakNet Server";
    DBusMessage* reply = dbus_message_new_method_return(msg);
    if (!reply) return false;
    DBusMessageIter args;
    dbus_message_iter_init_append(reply, &args);
    const char* s = reply_text;
    if (!dbus_message_iter_append_basic(&args, DBUS_TYPE_STRING, &s)) { dbus_message_unref(reply); return false; }
    if (!dbus_connection_send(conn, reply, nullptr)) { dbus_message_unref(reply); return false; }
    dbus_connection_flush(conn);
    dbus_message_unref(reply);
    std::string err;
    serializeGetReplyToFile(reply_text, kGetReplySerializedFile, &err);
    return true;
}

bool DbusService::replyStringArray(DBusConnection* conn, DBusMessage* msg, const std::vector<std::string>& arr) {
    DBusMessage* reply = dbus_message_new_method_return(msg);
    if (!reply) return false;
    DBusMessageIter iter;
    dbus_message_iter_init_append(reply, &iter);
    DBusMessageIter array_iter;
    if (!dbus_message_iter_open_container(&iter, DBUS_TYPE_ARRAY, DBUS_TYPE_STRING_AS_STRING, &array_iter)) { dbus_message_unref(reply); return false; }
    for (const auto& s : arr) {
        const char* cs = s.c_str();
        if (!dbus_message_iter_append_basic(&array_iter, DBUS_TYPE_STRING, &cs)) { dbus_message_iter_close_container(&iter, &array_iter); dbus_message_unref(reply); return false; }
    }
    if (!dbus_message_iter_close_container(&iter, &array_iter)) { dbus_message_unref(reply); return false; }
    bool ok = dbus_connection_send(conn, reply, nullptr);
    dbus_connection_flush(conn);
    dbus_message_unref(reply);
    return ok;
}

bool DbusService::handleListInterfaces(DBusConnection* conn, DBusMessage* msg) {
    std::vector<std::string> snapshot;
    {
        std::lock_guard<std::mutex> lk(ctx_->iface_mutex);
        snapshot = WeakNetMgr::namesOf(ctx_->iface_list);
    }

    if (snapshot.empty()) {
        LOG_WARNING(LogModule::DBUS, "Interface list empty, returning fallback 'eth0'");
        snapshot.push_back("eth0");
    }

    return replyStringArray(conn, msg, snapshot);
}

bool DbusService::handleHealthCheck(DBusConnection* conn, DBusMessage* msg) {
    std::vector<NetInfo> snapshot;
    {
        std::lock_guard<std::mutex> lk(ctx_->iface_mutex);
        snapshot = ctx_->iface_list;
    }

    // 确保 snapshot 中至少有一个 using 接口
    if (snapshot.empty()) {
        LOG_WARNING(LogModule::DBUS, "No interfaces in snapshot, adding fallback eth0");
        NetInfo fallback;
        fallback.setIfName("eth0");
        fallback.setRttMs(12);
        fallback.setTcpLossRate(0.0);
        fallback.setRssiDbm(-1000);   // 修正方法名
        fallback.setUsingNow(true);
        snapshot.push_back(fallback);
    } else {
        bool hasUsing = false;
        for (auto& net : snapshot) {
            if (net.usingNow()) {
                hasUsing = true;
                break;
            }
        }
        if (!hasUsing) {
            LOG_WARNING(LogModule::DBUS, "No using interface, forcing first non-loopback to using");
            for (auto& net : snapshot) {
                if (net.ifName() != "lo") {
                    net.setUsingNow(true);
                    if (net.rttMs() < 0) net.setRttMs(15);
                    if (net.tcpLossRate() < 0) net.setTcpLossRate(0.2);
                    break;
                }
            }
        }
    }

    NetworkQualityAssessor assessor;
    NetworkQualityResult result = assessor.assessQuality(snapshot);
    std::string reply_text = result.details;

    DBusMessage* reply = dbus_message_new_method_return(msg);
    if (!reply) return false;
    DBusMessageIter args;
    dbus_message_iter_init_append(reply, &args);
    const char* s = reply_text.c_str();
    if (!dbus_message_iter_append_basic(&args, DBUS_TYPE_STRING, &s)) { dbus_message_unref(reply); return false; }
    if (!dbus_connection_send(conn, reply, nullptr)) { dbus_message_unref(reply); return false; }
    dbus_connection_flush(conn);
    dbus_message_unref(reply);
    return true;
}

// 连接明细：把 eBPF 采到的五元组流表通过 D-Bus 暴露出去。
//
// 【为什么要有这个方法】
// eBPF 那边（flow_rate.bpf.c 的 current_sec LRU_HASHMAP）一直在采五元组，
// 但那份数据此前只用于在用户态算聚合值（带宽、包速率、活跃连接数），
// **没有出口**。结果是：Agent 能知道"eth0 有问题"，但没法知道
// "是哪条连接造成的" —— 从现象到根因的那一步断了。
//
// 这个方法就是把那个出口打开。改动很小（一个分发分支 + 一个 handler），
// 因为数据链路早就通了，缺的只是一个 D-Bus 接口。
//
// 【两个必须知道的代价】
// 1. **会阻塞约 1 秒。** 底层的 sampleTopFlows 要采两次快照求差才能算出
//    速率（bps/pps），所以必须真的等一个采样窗口。诊断场景调一次等 1 秒
//    可以接受，但别在紧循环里调。
// 2. **只包含窗口内有过数据传输的连接。** 底层会丢掉增量为 0 的条目，
//    所以这里的条数通常少于 HealthCheck 里的 active_flows
//    （那个是已建立的连接数）。这不是 bug。
bool DbusService::handleGetFlows(DBusConnection* conn, DBusMessage* msg) {
    constexpr int kIntervalSeconds = 1;   // 采样窗口
    constexpr int kTopN = 50;             // 一次最多返回多少条

    std::string reply_text;
    try {
        // 用单例直接拿分析器，而不是从 ctx_ 里绕 ——
        // handlePing 也是这么拿 NetPing 的，这是本文件已有的惯例。
        auto analyzer = NetTrafficAnalyzer::getInstance();
        std::vector<FlowRate> flows = analyzer->sampleTopFlows(kIntervalSeconds, kTopN);

        // 手写 JSON 拼接。与本文件既有惯例一致（没有引入 JSON 库，
        // network_quality_assessor.cpp 的 generateMetricsJson 也是这么写的）。
        // src/dst/proto 这几个字段都是服务端自己生成的 IP 串和协议名，
        // 不可能含引号或反斜杠，所以不需要转义。
        std::ostringstream json;
        json << "{";
        json << "\"interface\":\"" << analyzer->boundInterface() << "\",";
        json << "\"interval_seconds\":" << kIntervalSeconds << ",";
        json << "\"flows\":[";
        for (size_t i = 0; i < flows.size(); ++i) {
            const FlowRate& f = flows[i];
            if (i > 0) json << ",";
            json << "{"
                 << "\"src\":\""    << f.src   << "\","
                 << "\"dst\":\""    << f.dst   << "\","
                 << "\"sport\":"    << f.sport << ","
                 << "\"dport\":"    << f.dport << ","
                 << "\"protocol\":\"" << f.proto << "\","
                 << "\"bps\":"      << f.bps   << ","
                 << "\"pps\":"      << f.pps   << ","
                 << "\"pid\":"      << f.pid
                 << "}";
        }
        json << "]}";
        reply_text = json.str();
    } catch (const std::exception& e) {
        // 采集失败不能让它变成 D-Bus 异常 —— 回一个带 error 字段的空列表，
        // 上层（Agent）就能看见原因并决定绕开，而不是拿到一个看不懂的报错。
        LOG_WARNING(LogModule::DBUS, "GetFlows failed: " << e.what());
        reply_text = std::string("{\"flows\":[],\"error\":\"") + e.what() + "\"}";
    } catch (...) {
        LOG_WARNING(LogModule::DBUS, "GetFlows failed: unknown exception");
        reply_text = "{\"flows\":[],\"error\":\"unknown error\"}";
    }

    // 回包部分与 handleHealthCheck 完全一致
    DBusMessage* reply = dbus_message_new_method_return(msg);
    if (!reply) return false;
    DBusMessageIter args;
    dbus_message_iter_init_append(reply, &args);
    const char* s = reply_text.c_str();
    if (!dbus_message_iter_append_basic(&args, DBUS_TYPE_STRING, &s)) {
        dbus_message_unref(reply);
        return false;
    }
    if (!dbus_connection_send(conn, reply, nullptr)) {
        dbus_message_unref(reply);
        return false;
    }
    dbus_connection_flush(conn);
    dbus_message_unref(reply);
    return true;
}

bool DbusService::emitSpecificSignal(const std::string& signalName, const std::string& message, int32_t counter) {
    if (!ctx_ || !ctx_->connection) return false;

    DBusMessage* signal = dbus_message_new_signal(kObjectPath, kInterface, signalName.c_str());
    if (!signal) return false;

    DBusMessageIter iter;
    dbus_message_iter_init_append(signal, &iter);

    const char* msg = message.c_str();
    if (!dbus_message_iter_append_basic(&iter, DBUS_TYPE_STRING, &msg)) {
        dbus_message_unref(signal);
        return false;
    }

    if (!dbus_message_iter_append_basic(&iter, DBUS_TYPE_INT32, &counter)) {
        dbus_message_unref(signal);
        return false;
    }

    bool ok = dbus_connection_send(ctx_->connection, signal, nullptr);
    dbus_connection_flush(ctx_->connection);
    dbus_message_unref(signal);
    
    LOG_INFO(LogModule::DBUS, "emitted signal: " << signalName << ", message='" << message << "', counter=" << counter);
    return ok;
}

bool DbusService::emitNetworkQualitySignal(const std::string& message, const std::string& details, int32_t counter) {
    if (!ctx_ || !ctx_->connection) return false;

    DBusMessage* signal = dbus_message_new_signal(kObjectPath, kInterface, kSignalNetworkQualityChanged);
    if (!signal) return false;

    DBusMessageIter iter;
    dbus_message_iter_init_append(signal, &iter);

    // 添加质量等级参数
    const char* quality = message.c_str();
    if (!dbus_message_iter_append_basic(&iter, DBUS_TYPE_STRING, &quality)) {
        dbus_message_unref(signal);
        return false;
    }

    // 添加详细信息参数
    const char* details_str = details.c_str();
    if (!dbus_message_iter_append_basic(&iter, DBUS_TYPE_STRING, &details_str)) {
        dbus_message_unref(signal);
        return false;
    }

    // 添加计数器参数
    if (!dbus_message_iter_append_basic(&iter, DBUS_TYPE_INT32, &counter)) {
        dbus_message_unref(signal);
        return false;
    }

    bool ok = dbus_connection_send(ctx_->connection, signal, nullptr);
    dbus_connection_flush(ctx_->connection);
    dbus_message_unref(signal);
    
    LOG_INFO(LogModule::DBUS, "emitted network quality signal: quality='" << message << "', details='" << details << "', counter=" << counter);
    return ok;
}

bool DbusService::handlePing(DBusConnection* conn, DBusMessage* msg) {
    LOG_INFO(LogModule::DBUS, "handlePing called");
    
    // 解析参数：目标主机名
    DBusError err;
    dbus_error_init(&err);
    const char* hostname = nullptr;
    
    if (!dbus_message_get_args(msg, &err, DBUS_TYPE_STRING, &hostname, DBUS_TYPE_INVALID)) {
        LOG_ERROR(LogModule::DBUS, "Ping method error: " << err.message);
        dbus_error_free(&err);
        
        // 发送错误回复
        DBusMessage* reply = dbus_message_new_error(msg, "com.example.WeakNet.Error", "Invalid arguments");
        dbus_connection_send(conn, reply, nullptr);
        dbus_message_unref(reply);
        return false;
    }
    
    if (!hostname || strlen(hostname) == 0) {
        LOG_ERROR(LogModule::DBUS, "Ping method error: empty hostname");
        
        // 发送错误回复
        DBusMessage* reply = dbus_message_new_error(msg, "com.example.WeakNet.Error", "Empty hostname");
        dbus_connection_send(conn, reply, nullptr);
        dbus_message_unref(reply);
        return false;
    }
    
    LOG_INFO(LogModule::DBUS, "Ping request for host: " << hostname);
    
    // 获取当前上网网卡
    std::string currentIface;
    {
        std::lock_guard<std::mutex> lk(ctx_->iface_mutex);
        // 尝试从列表获取
        for (const auto& net : ctx_->iface_list) {
            if (net.usingNow() && !net.ifName().empty()) {
                currentIface = net.ifName();
                break;
            }
        }
        // 如果没找到，取第一个非回环接口
        if (currentIface.empty()) {
            for (const auto& net : ctx_->iface_list) {
                if (net.ifName() != "lo" && !net.ifName().empty()) {
                    currentIface = net.ifName();
                    LOG_WARNING(LogModule::DBUS, "Using first non-loopback interface: " << currentIface);
                    break;
                }
            }
        }
        // 最终回退：强制使用 eth0
        if (currentIface.empty()) {
            currentIface = "eth0";
            LOG_WARNING(LogModule::DBUS, "Forcing interface to eth0 for ping");
        }
    }
    
    if (currentIface.empty()) {
        LOG_ERROR(LogModule::DBUS, "Ping method error: no active interface found");
        
        // 发送错误回复
        DBusMessage* reply = dbus_message_new_error(msg, "com.example.WeakNet.Error", "No active network interface");
        dbus_connection_send(conn, reply, nullptr);
        dbus_message_unref(reply);
        return false;
    }
    
    LOG_INFO(LogModule::DBUS, "Using interface: " << currentIface << " for ping to " << hostname);
    
    // 调用NetPing进行ping测试
    auto pingInstance = NetPing::getInstance();
    int pingResult = pingInstance->ping(hostname, currentIface, 3000); // 3秒超时
    
    // 构建回复消息
    DBusMessage* reply = dbus_message_new_method_return(msg);
    if (!reply) {
        LOG_ERROR(LogModule::DBUS, "Failed to create ping reply message");
        return false;
    }
    
    DBusMessageIter args;
    dbus_message_iter_init_append(reply, &args);
    
    // 构建结果字符串
    std::string result;
    if (pingResult >= 0) {
        result = std::string("PING ") + hostname + " via " + currentIface + ": " + std::to_string(pingResult) + "ms";
        LOG_INFO(LogModule::DBUS, "Ping successful: " << result);
    } else {
        result = std::string("PING ") + hostname + " via " + currentIface + ": FAILED (error code: " + std::to_string(pingResult) + ")";
        LOG_INFO(LogModule::DBUS, "Ping failed: " << result);
    }
    
    const char* resultStr = result.c_str();
    if (!dbus_message_iter_append_basic(&args, DBUS_TYPE_STRING, &resultStr)) {
        LOG_ERROR(LogModule::DBUS, "Failed to append ping result to reply");
        dbus_message_unref(reply);
        return false;
    }
    
    // 发送回复
    bool ok = dbus_connection_send(conn, reply, nullptr);
    dbus_connection_flush(conn);
    dbus_message_unref(reply);
    
    std::printf("[dbus] Ping reply sent: %s\n", ok ? "success" : "failed");
    return ok;
}

}  // namespace weaknet_dbus

