#include "using_iface.h"

#include <atomic>
#include <thread>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <algorithm>
#include <cstring>
#include <iostream>

#if defined(__linux__)
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <linux/if_link.h>
#include <net/if.h>
#include <stdexcept>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

std::once_flag UsingInterfaceManager::s_onceFlag;
std::shared_ptr<UsingInterfaceManager> UsingInterfaceManager::s_instance;

std::shared_ptr<UsingInterfaceManager> UsingInterfaceManager::getInstance() {
    std::call_once(s_onceFlag, [](){ s_instance = std::shared_ptr<UsingInterfaceManager>(new UsingInterfaceManager()); });
    return s_instance;
}

UsingInterfaceManager::UsingInterfaceManager() = default;
UsingInterfaceManager::~UsingInterfaceManager() = default;

struct UsingInterfaceManager::Impl {
#if defined(__linux__)
    int nlSocket = -1;
    std::thread worker;
    std::atomic<bool> running{false};
    std::unordered_map<int, std::string> ifindexToName;
    std::unordered_set<int> upIfaces;
    std::unordered_set<int> v4Default;
    std::unordered_set<int> v6Default;

    static void setNonBlocking(int fd) {
        int flags = fcntl(fd, F_GETFL, 0);
        if (flags == -1) throw std::runtime_error("fcntl(F_GETFL) failed");
        if (fcntl(fd, F_SETFL, flags | O_NONBLOCK) == -1) throw std::runtime_error("fcntl(F_SETFL) failed");
    }

    void openSocket() {
        nlSocket = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
        if (nlSocket < 0) throw std::runtime_error("socket(AF_NETLINK) failed");
        sockaddr_nl addr{};
        addr.nl_family = AF_NETLINK;
        addr.nl_groups = RTMGRP_LINK | RTMGRP_IPV4_ROUTE | RTMGRP_IPV6_ROUTE;
        if (bind(nlSocket, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
            throw std::runtime_error("bind(AF_NETLINK) failed");
        }
        setNonBlocking(nlSocket);
    }

    void sendReq(uint16_t type, uint16_t flags, uint8_t family) {
        struct { nlmsghdr nlh; rtgenmsg gen; } req{};
        req.nlh.nlmsg_len = sizeof(req);
        req.nlh.nlmsg_type = type;
        req.nlh.nlmsg_flags = flags | NLM_F_REQUEST;
        req.nlh.nlmsg_seq = static_cast<uint32_t>(::time(nullptr));
        req.nlh.nlmsg_pid = 0;
        req.gen.rtgen_family = family;
        sockaddr_nl nladdr{}; nladdr.nl_family = AF_NETLINK;
        struct iovec iov{ &req, sizeof(req) };
        struct msghdr msg{}; msg.msg_name = &nladdr; msg.msg_namelen = sizeof(nladdr); msg.msg_iov = &iov; msg.msg_iovlen = 1;
        if (sendmsg(nlSocket, &msg, 0) < 0) throw std::runtime_error("sendmsg failed");
    }

    void dumpInitial() {
        sendReq(RTM_GETLINK, NLM_F_DUMP, AF_PACKET);
        recvDump();
        sendReq(RTM_GETROUTE, NLM_F_DUMP, AF_INET);
        recvDump();
        sendReq(RTM_GETROUTE, NLM_F_DUMP, AF_INET6);
        recvDump();
    }

    template <typename T, size_t N>
    static void parseAttrs(struct rtattr* rta, int len, T (&attrs)[N]) {
        std::fill(std::begin(attrs), std::end(attrs), nullptr);
        while (RTA_OK(rta, len)) {
            if (rta->rta_type < N) attrs[rta->rta_type] = rta;
            rta = RTA_NEXT(rta, len);
        }
    }

    void handleLink(ifinfomsg* info, void* attrHead, int attrLen) {
        struct rtattr* attrs[IFLA_MAX + 1];
        parseAttrs(reinterpret_cast<struct rtattr*>(attrHead), attrLen, attrs);
        int ifindex = info->ifi_index;
        if (attrs[IFLA_IFNAME]) {
            char name[IFNAMSIZ]{};
            std::snprintf(name, sizeof(name), "%s", reinterpret_cast<char*>(RTA_DATA(attrs[IFLA_IFNAME])));
            ifindexToName[ifindex] = name;
        }
        bool isLoopback = (info->ifi_flags & IFF_LOOPBACK) != 0;
        bool isUp = (info->ifi_flags & IFF_UP) != 0;
        if (isUp && !isLoopback) upIfaces.insert(ifindex); else upIfaces.erase(ifindex);
        if (info->ifi_change == ~0U || (info->ifi_flags == 0)) {
            v4Default.erase(ifindex);
            v6Default.erase(ifindex);
        }
    }

    static bool isDefaultRoute(const rtmsg* rtm) {
        return rtm->rtm_dst_len == 0 &&
               (rtm->rtm_table == RT_TABLE_MAIN || rtm->rtm_table == RT_TABLE_DEFAULT || rtm->rtm_table == RT_TABLE_UNSPEC) &&
               (rtm->rtm_scope == RT_SCOPE_UNIVERSE || rtm->rtm_scope == RT_SCOPE_NOWHERE || rtm->rtm_scope == RT_SCOPE_SITE);
    }

    void handleRoute(rtmsg* rtm, void* attrHead, int attrLen, int nlmsgType) {
        struct rtattr* attrs[RTA_MAX + 1];
        parseAttrs(reinterpret_cast<struct rtattr*>(attrHead), attrLen, attrs);
        if (!isDefaultRoute(rtm)) return;
        int oif = -1; bool hasGw = false;
        if (attrs[RTA_OIF]) oif = *reinterpret_cast<int*>(RTA_DATA(attrs[RTA_OIF]));
        if (attrs[RTA_GATEWAY]) hasGw = true;
        if (oif <= 0 || !hasGw) return;
        auto& target = (rtm->rtm_family == AF_INET) ? v4Default : v6Default;
        if (nlmsgType == RTM_NEWROUTE) target.insert(oif); else target.erase(oif);
    }

    void dispatch(nlmsghdr* hdr) {
        switch (hdr->nlmsg_type) {
            case RTM_NEWLINK:
            case RTM_DELLINK: {
                auto* msg = reinterpret_cast<ifinfomsg*>(NLMSG_DATA(hdr));
                void* attrsHead = IFLA_RTA(msg);
                int attrsLen = IFLA_PAYLOAD(hdr);
                handleLink(msg, attrsHead, attrsLen);
                break;
            }
            case RTM_NEWROUTE:
            case RTM_DELROUTE: {
                auto* msg = reinterpret_cast<rtmsg*>(NLMSG_DATA(hdr));
                void* attrsHead = RTM_RTA(msg);
                int attrsLen = RTM_PAYLOAD(hdr);
                handleRoute(msg, attrsHead, attrsLen, hdr->nlmsg_type);
                break;
            }
            default:
                break;
        }
    }

    void recvDump() {
        std::vector<char> buf(64 * 1024);
        while (true) {
            sockaddr_nl nladdr{};
            struct iovec iov{ buf.data(), buf.size() };
            struct msghdr msg{}; msg.msg_name = &nladdr; msg.msg_namelen = sizeof(nladdr); msg.msg_iov = &iov; msg.msg_iovlen = 1;
            ssize_t len = recvmsg(nlSocket, &msg, 0);
            if (len < 0) {
                if (errno == EINTR) continue;
                if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                throw std::runtime_error("recvmsg failed");
            }
            if (len == 0) break;
            for (nlmsghdr* hdr = reinterpret_cast<nlmsghdr*>(buf.data()); NLMSG_OK(hdr, static_cast<unsigned>(len)); hdr = NLMSG_NEXT(hdr, len)) {
                if (hdr->nlmsg_type == NLMSG_DONE) return;
                if (hdr->nlmsg_type == NLMSG_ERROR) continue;
                dispatch(hdr);
            }
        }
    }

    void publishState(UsingInterfaceManager* owner, bool printLog) {
        uint32_t methodFlags = 0;
        int chosen = -1;
        if (!v4Default.empty()) {
            for (int idx : v4Default) { if (upIfaces.count(idx)) { chosen = idx; methodFlags |= UsingMethodFlag::IPv4Default; break; } }
        }
        if (!v6Default.empty()) {
            for (int idx : v6Default) { if (upIfaces.count(idx)) { if (chosen == -1) chosen = idx; methodFlags |= UsingMethodFlag::IPv6Default; break; } }
        }
        std::string ifname;
        if (chosen != -1) {
            auto it = ifindexToName.find(chosen);
            if (it != ifindexToName.end()) ifname = it->second; else ifname = std::string("ifindex=") + std::to_string(chosen);
        }
        {
            std::lock_guard<std::mutex> lk(owner->stateMutex_);
            owner->currentIfName_ = ifname;
            owner->methodFlags_ = methodFlags;
        }
        if (printLog) {
            if (!ifname.empty()) {
                std::cout << "[net] 当前上网网卡: " << ifname
                          << " flags=" << ((methodFlags & UsingMethodFlag::IPv4Default) ? "IPv4" : "")
                          << (((methodFlags & UsingMethodFlag::IPv4Default) && (methodFlags & UsingMethodFlag::IPv6Default)) ? "+" : "")
                          << ((methodFlags & UsingMethodFlag::IPv6Default) ? "IPv6" : "")
                          << std::endl;
            } else {
                std::cout << "[net] 当前上网网卡: (none)" << std::endl;
            }
        }
    }

    void eventLoop(UsingInterfaceManager* owner) {
        try {
            openSocket();
            dumpInitial();
            // 发布一次初始状态，避免刚启动阶段为空
            publishState(owner, /*printLog=*/true);
            std::vector<char> buf(64 * 1024);
            running.store(true);
            while (running.load()) {
                sockaddr_nl nladdr{};
                struct iovec iov{ buf.data(), buf.size() };
                struct msghdr msg{}; msg.msg_name = &nladdr; msg.msg_namelen = sizeof(nladdr); msg.msg_iov = &iov; msg.msg_iovlen = 1;
                ssize_t len = recvmsg(nlSocket, &msg, 0);
                if (len < 0) {
                    if (errno == EINTR) continue;
                    if (errno == EAGAIN || errno == EWOULDBLOCK) { usleep(1000 * 50); continue; }
                    throw std::runtime_error("recvmsg failed");
                }
                if (len == 0) continue;
                bool seenDone = false;
                for (nlmsghdr* hdr = reinterpret_cast<nlmsghdr*>(buf.data()); NLMSG_OK(hdr, static_cast<unsigned>(len)); hdr = NLMSG_NEXT(hdr, len)) {
                    if (hdr->nlmsg_type == NLMSG_DONE) { seenDone = true; break; }
                    if (hdr->nlmsg_type == NLMSG_ERROR) continue;
                    dispatch(hdr);
                }
                if (seenDone) continue;
                // 每批处理后发布最新状态
                publishState(owner, /*printLog=*/true);
            }
        } catch (const std::exception& ex) {
            std::cerr << "[net] loop error: " << ex.what() << std::endl;
        }
        if (nlSocket >= 0) close(nlSocket);
        nlSocket = -1;
        running.store(false);
    }
#else
    void eventLoop(UsingInterfaceManager*) {}
#endif
};

void UsingInterfaceManager::start() {
#if defined(__linux__)
    std::lock_guard<std::mutex> lk(stateMutex_);
    if (impl_ == nullptr) impl_ = new Impl();
    if (!impl_->running.load()) {
        impl_->worker = std::thread([this]{ impl_->eventLoop(this); });
        impl_->worker.detach();
    }
#else
    (void)stateMutex_;
#endif
}

std::string UsingInterfaceManager::getCurrentInterface() {
    std::lock_guard<std::mutex> lk(stateMutex_);
    return currentIfName_;
}

uint32_t UsingInterfaceManager::getMethodFlags() {
    std::lock_guard<std::mutex> lk(stateMutex_);
    return methodFlags_;
}
