#include "net_ping.h"

#if defined(__linux__)
#include <arpa/inet.h>
#include <errno.h>
#include <net/if.h>
#include <netdb.h>
#include <netinet/ip.h>
#include <netinet/ip_icmp.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <unistd.h>
#include <cstring>
#include <chrono>
#include <string>

namespace {
static constexpr int kPacketSize = 4096;
}

std::once_flag NetPing::s_onceFlag;
std::shared_ptr<NetPing> NetPing::s_instance;

std::shared_ptr<NetPing> NetPing::getInstance() {
    std::call_once(s_onceFlag, [](){ s_instance = std::shared_ptr<NetPing>(new NetPing()); });
    return s_instance;
}

NetPing::NetPing() = default;
NetPing::~NetPing() = default;

void NetPing::Init() {}
void NetPing::Shutdown() {}

uint16_t NetPing::checksum(uint16_t* addr, int len) {
    int nleft = len;
    uint32_t sum = 0;
    uint16_t* w = addr;
    uint16_t answer = 0;

    while (nleft > 1) {
        sum += *w++;
        nleft -= 2;
    }
    if (nleft == 1) {
        uint16_t last = 0;
        *reinterpret_cast<uint8_t*>(&last) = *reinterpret_cast<uint8_t*>(w);
        sum += last;
    }
    sum = (sum >> 16) + (sum & 0xffff);
    sum += (sum >> 16);
    answer = static_cast<uint16_t>(~sum);
    return answer;
}

bool NetPing::resolveHostIPv4(const std::string& host, struct sockaddr_in& out) {
    struct addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_RAW;
    struct addrinfo* res = nullptr;
    int ret = getaddrinfo(host.c_str(), nullptr, &hints, &res);
    if (ret != 0) return false;
    out = *reinterpret_cast<struct sockaddr_in*>(res->ai_addr);
    freeaddrinfo(res);
    return true;
}

int NetPing::packIcmp(struct icmp* icmp, uint16_t id, uint16_t seq) {
    std::memset(icmp, 0, sizeof(struct icmp));
    icmp->icmp_type = ICMP_ECHO;
    icmp->icmp_code = 0;
    icmp->icmp_id = id;
    icmp->icmp_seq = seq;
    struct timeval* tv = reinterpret_cast<struct timeval*>(icmp->icmp_data);
    gettimeofday(tv, nullptr);
    icmp->icmp_cksum = 0;
    icmp->icmp_cksum = checksum(reinterpret_cast<uint16_t*>(icmp), sizeof(struct icmp));
    return sizeof(struct icmp);
}

int NetPing::ping(const std::string& host, const std::string& ifaceName, int timeoutMs) {
    int sockfd = socket(AF_INET, SOCK_RAW, IPPROTO_ICMP);
    if (sockfd < 0) {
        return -1;
    }

    // Bind to device
    struct ifreq ifr{};
    std::snprintf(ifr.ifr_name, IFNAMSIZ, "%s", ifaceName.c_str());
    if (setsockopt(sockfd, SOL_SOCKET, SO_BINDTODEVICE, &ifr, sizeof(ifr)) < 0) {
        close(sockfd);
        return -2;
    }

    // Resolve destination
    struct sockaddr_in dest{};
    if (!resolveHostIPv4(host, dest)) {
        close(sockfd);
        return -3;
    }

    // Optional: enlarge recv buffer
    int recvBuf = 64 * 1024;
    setsockopt(sockfd, SOL_SOCKET, SO_RCVBUF, &recvBuf, sizeof(recvBuf));

    // Build ICMP
    struct icmp icmpPacket{};
    uint16_t id = static_cast<uint16_t>(getpid());
    static uint16_t seq = 0;
    packIcmp(&icmpPacket, id, ++seq);

    // Send and measure send duration
    auto t0 = std::chrono::steady_clock::now();
    ssize_t sent = sendto(sockfd, &icmpPacket, sizeof(icmpPacket), 0,
                          reinterpret_cast<struct sockaddr*>(&dest), sizeof(dest));
    if (sent < 0) {
        close(sockfd);
        return -4;
    }
    auto t1 = std::chrono::steady_clock::now();
    int sendMs = static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());

    // Wait for reply
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(sockfd, &rfds);
    struct timeval tv{};
    tv.tv_sec = timeoutMs / 1000;
    tv.tv_usec = (timeoutMs % 1000) * 1000;

    int rv = select(sockfd + 1, &rfds, nullptr, nullptr, &tv);
    if (rv <= 0) {
        close(sockfd);
        return (rv == 0) ? -5 : -6; // timeout / select error
    }

    char buf[kPacketSize];
    struct sockaddr_in src{};
    socklen_t slen = sizeof(src);
    ssize_t n = recvfrom(sockfd, buf, sizeof(buf), 0, reinterpret_cast<struct sockaddr*>(&src), &slen);
    if (n <= 0) {
        close(sockfd);
        return -7;
    }

    // Kernel may include IP header before ICMP; locate ICMP by skipping IPv4 header
    struct ip* iphdr = reinterpret_cast<struct ip*>(buf);
    int iphdrlen = iphdr->ip_hl * 4;
    if (n < iphdrlen + static_cast<ssize_t>(sizeof(struct icmp))) {
        close(sockfd);
        return -8;
    }
    struct icmp* ricmp = reinterpret_cast<struct icmp*>(buf + iphdrlen);
    if (ricmp->icmp_type != ICMP_ECHOREPLY || ricmp->icmp_id != id) {
        close(sockfd);
        return -9;
    }

    // RTT based on embedded send time
    struct timeval* sendTv = reinterpret_cast<struct timeval*>(ricmp->icmp_data);
    struct timeval now{};
    gettimeofday(&now, nullptr);
    long rttMs = (now.tv_sec - sendTv->tv_sec) * 1000 + (now.tv_usec - sendTv->tv_usec) / 1000;

    close(sockfd);
    return static_cast<int>(rttMs >= 0 ? rttMs : sendMs);
}

#else

// 非Linux平台的空实现
NetPing::NetPing() {}
NetPing::~NetPing() {}
void NetPing::Init() {}
void NetPing::Shutdown() {}
uint16_t NetPing::checksum(uint16_t*, int) { return 0; }
bool NetPing::resolveHostIPv4(const std::string&, struct sockaddr_in&) { return false; }
int NetPing::packIcmp(struct icmp*, uint16_t, uint16_t) { return 0; }
int NetPing::ping(const std::string&, const std::string&, int) { return -1; }

#endif
