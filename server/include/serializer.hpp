// serializer.hpp
// 提供简单的序列化/反序列化工具，用于将信号与Get返回值持久化到文件

#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace weaknet_dbus {

// Changed 信号的载荷结构
struct ChangedPayload {
    std::string message;   // 信号文本消息
    int32_t counter;       // 计数器，用于演示随时间变化
};

// 将内存缓冲写入文件（覆盖写）
// 成功返回 true，失败返回 false
bool writeBufferToFile(const std::vector<uint8_t>& buffer, const std::string& filepath, std::string* error_message);

// 从文件读出全部内容到缓冲区
// 成功返回 true，失败返回 false
bool readFileToBuffer(const std::string& filepath, std::vector<uint8_t>* buffer, std::string* error_message);

// 序列化字符串为: [u32长度][字节...]
void serializeString(const std::string& value, std::vector<uint8_t>& out_buffer);

// 从缓冲区偏移处反序列化字符串，读取后推进 offset
bool deserializeString(const std::vector<uint8_t>& buffer, size_t& offset, std::string& out_value);

// 以小端序写入/读取 int32
void serializeInt32(int32_t value, std::vector<uint8_t>& out_buffer);
bool deserializeInt32(const std::vector<uint8_t>& buffer, size_t& offset, int32_t& out_value);

// 将 Get 方法的回复（字符串）序列化到文件
bool serializeGetReplyToFile(const std::string& reply, const std::string& filepath, std::string* error_message);

// 从文件反序列化 Get 方法的回复
bool deserializeGetReplyFromFile(const std::string& filepath, std::string* out_reply, std::string* error_message);

// 将 Changed 信号的载荷序列化到文件
bool serializeChangedPayloadToFile(const ChangedPayload& payload, const std::string& filepath, std::string* error_message);

// 从文件反序列化 Changed 信号的载荷
bool deserializeChangedPayloadFromFile(const std::string& filepath, ChangedPayload* out_payload, std::string* error_message);

}  // namespace weaknet_dbus


