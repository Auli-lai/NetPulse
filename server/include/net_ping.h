#pragma once

#include <string>
#include <cstdint>
#include <memory>
#include <mutex>

class NetPing {
public:
    NetPing();
    ~NetPing();

    // 单例获取（线程安全懒汉，共享所有者）
    static std::shared_ptr<NetPing> getInstance();

    // 以指定网卡名对目标主机名执行 ICMP Echo，返回 RTT(ms)，失败返回负值
    int ping(const std::string& host, const std::string& ifaceName, int timeoutMs = 1000);

    // 可选初始化/关闭
    void Init();
    void Shutdown();

private:
    // helpers
    static uint16_t checksum(uint16_t* addr, int len);
    static bool resolveHostIPv4(const std::string& host, struct sockaddr_in& out);
    static int packIcmp(struct icmp* icmp, uint16_t id, uint16_t seq);

    static std::once_flag s_onceFlag;
    static std::shared_ptr<NetPing> s_instance;
};
