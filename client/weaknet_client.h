// weaknet_client.h
// WeakNet 客户端动态库 C API 接口
// 用于与其他程序集成WeakNet监控和事件监听功能

#ifndef WEAKNET_CLIENT_H
#define WEAKNET_CLIENT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * WeakNet 客户端动态库 API
 * 
 * 此库提供与WeakNet D-Bus服务端通信的C接口，包括：
 * - 网络接口信息获取
 * - 网络状态监控
 * - 事件监听和订阅
 * - TCP丢包率监控
 * - RTT延迟监控
 * 
 * 使用示例：
 *   weaknet_init();                          // 初始化
 *   weaknet_get_interfaces(buf, sz, err, errsz);  // 获取信息
 *   weaknet_subscribe_event("InterfaceChanged", callback);  // 订阅事件
 *   weaknet_check_events(...);                // 检查事件
 *   weaknet_cleanup();                       // 清理
 */

// ============== 库初始化和清理 ==============

/**
 * 初始化WeakNet客户端库
 * 必须在调用其他任何函数之前调用此函数
 * 
 * @return true-成功, false-失败
 */
bool weaknet_init();

/**
 * 清理WeakNet客户端资源
 * 应在应用程序退出前调用此函数
 */
void weaknet_cleanup();

/**
 * 检查客户端连接状态
 * 
 * @return true-已连接, false-未连接
 */
bool weaknet_is_connected();

// ============== 网络接口信息获取 ==============

/**
 * 获取当前网络接口信息
 * 
 * @param buffer 结果缓冲区，将存储网络接口信息
 * @param buffer_size 缓冲区大小
 * @param error_buffer 错误信息缓冲区（如果调用失败）
 * @param error_size 错误信息缓冲区大小
 * @return true-成功, false-失败
 */
bool weaknet_get_interfaces(char* buffer, size_t buffer_size, char* error_buffer, size_t error_size);

/**
 * 网络健康检查
 * 
 * @param result_buffer 结果缓冲区，将存储健康检查结果
 * @param result_size 结果缓冲区大小
 * @param error_buffer 错误信息缓冲区（如果调用失败）
 * @param error_size 错误信息缓冲区大小
 * @return true-成功, false-失败
 */
bool weaknet_health_check(char* result_buffer, size_t result_size, char* error_buffer, size_t error_size);

/**
 * 从序列化文件读取最新状态（离线模式）
 * 
 * @param buffer 结果缓冲区，将存储文件中的内容
 * @param buffer_size 缓冲区大小
 * @param error_buffer 错误信息缓冲区（如果调用失败）
 * @param error_size 错误信息缓冲区大小
 * @return true-成功, false-失败
 */
bool weaknet_get_from_file(char* buffer, size_t buffer_size, char* error_buffer, size_t error_size);

/**
 * Ping指定主机（通过当前上网网卡）
 * 
 * @param hostname 目标主机名或IP地址
 * @param result_buffer 结果缓冲区，将存储ping结果
 * @param result_size 结果缓冲区大小
 * @param error_buffer 错误信息缓冲区（如果调用失败）
 * @param error_size 错误信息缓冲区大小
 * @return true-成功, false-失败
 */
bool weaknet_ping_host(const char* hostname, char* result_buffer, size_t result_size, char* error_buffer, size_t error_size);

// ============== 网络状态变化监控 ==============

/**
 * 检查网络状态变化（非阻塞）
 * 
 * @param message_buffer 消息缓冲区，将存储变化消息
 * @param message_size 消息缓冲区大小
 * @param counter 计数器（输出参数）
 * @param error_buffer 错误信息缓冲区（如果没有变化）
 * @param error_size 错误信息缓冲区大小
 * @return true-有变化, false-无变化或错误
 */
bool weaknet_check_changes(char* message_buffer, size_t message_size, int32_t* counter, char* error_buffer, size_t error_size);

// ============== 事件监听和订阅系统 ==============

/**
 * 事件回调函数类型
 * 
 * @param event_type 事件类型（如"InterfaceChanged"）
 * @param message 事件消息内容
 * @param counter 事件计数器
 * @param source 事件来源
 */
typedef void weaknet_event_callback_t(const char* event_type, const char* message, int32_t counter, const char* source);

/**
 * 网络质量事件回调函数类型
 * 
 * @param quality 网络质量等级（如"poor", "fair", "good", "excellent"）
 * @param details 详细质量信息（JSON格式的指标数据）
 * @param counter 事件计数器
 * @return true-继续监听, false-停止监听
 */
typedef bool (*weaknet_network_quality_callback_t)(const char* quality, const char* details, int32_t counter);

/**
 * 订阅特定事件类型
 * 
 * @param event_type 要订阅的事件类型（如"InterfaceChanged", "ConnectionModeChanged"）
 * @param callback 事件回调函数（可为nullptr，此时只添加DBus订阅）
 * @return true-成功, false-失败
 */
bool weaknet_subscribe_event(const char* event_type, weaknet_event_callback_t callback);

/**
 * 取消订阅事件
 * 
 * @param event_type 要取消订阅的事件类型
 * @return true-成功, false-失败
 */
bool weaknet_unsubscribe_event(const char* event_type);

/**
 * 获取支持的事件类型列表
 * 
 * @param buffer 结果缓冲区，将存储事件类型列表（逗号分隔）
 * @param buffer_size 缓冲区大小
 * @param error_buffer 错误信息缓冲区（如果调用失败）
 * @param error_size 错误信息缓冲区大小
 * @return true-成功, false-失败
 */
bool weaknet_get_event_types(char* buffer, size_t buffer_size, char* error_buffer, size_t error_size);

/**
 * 非阻塞检查事件
 * 
 * @param event_type_buffer 事件类型缓冲区（输出参数）
 * @param event_type_size 事件类型缓冲区大小
 * @param message_buffer 消息缓冲区（输出参数）
 * @param message_size 消息缓冲区大小
 * @param counter 计数器（输出参数）
 * @param source_buffer 事件来源缓冲区（输出参数）
 * @param source_size 事件来源缓冲区大小
 * @param error_buffer 错误信息缓冲区（如果没有事件）
 * @param error_size 错误信息缓冲区大小
 * @return true-检测到事件, false-没有事件或错误
 */
bool weaknet_check_events(char* event_type_buffer, size_t event_type_size,
                          char* message_buffer, size_t message_size,
                          int32_t* counter, char* source_buffer, size_t source_size,
                          char* error_buffer, size_t error_size);

// ============== 网络质量监控 ==============

/**
 * 订阅网络质量事件
 * 
 * @param callback 网络质量事件回调函数
 * @return true-成功, false-失败
 */
bool weaknet_subscribe_network_quality(weaknet_network_quality_callback_t callback);

/**
 * 非阻塞检查网络质量事件
 * 
 * @param quality_buffer 网络质量等级缓冲区（输出）
 * @param quality_size 质量等级缓冲区大小
 * @param details_buffer 详细质量信息缓冲区（输出）
 * @param details_size 详细信息缓冲区大小
 * @param counter 事件计数器（输出）
 * @param error_buffer 错误信息缓冲区（如果没有事件）
 * @param error_size 错误信息缓冲区大小
 * @return true-有质量事件, false-无事件或错误
 */
bool weaknet_check_network_quality(char* quality_buffer, size_t quality_size,
                                   char* details_buffer, size_t details_size, 
                                   int32_t* counter, char* error_buffer, size_t error_size);

// ============== 版本和状态信息 ==============

/**
 * 获取WeakNet客户端库版本信息
 * 
 * @param buffer 结果缓冲区
 * @param buffer_size 缓冲区大小
 * @return true-成功, false-失败
 */
bool weaknet_get_version(char* buffer, size_t buffer_size);

/**
 * 获取库的编译时间和编译选项信息
 * 
 * @param buffer 结果缓冲区
 * @param buffer_size 缓冲区大小
 * @return true-成功, false-失败
 */
bool weaknet_get_build_info(char* buffer, size_t buffer_size);

#ifdef __cplusplus
}
#endif

#endif // WEAKNET_CLIENT_H