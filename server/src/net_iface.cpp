
#if defined(__linux__)
#include "net_iface.h"
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

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#ifndef IFF_LOWER_UP
#define IFF_LOWER_UP 0x10000
#endif
#ifndef IFF_DORMANT
#define IFF_DORMANT 0x20000
#endif

namespace {

struct NetlinkMessageBuffer {
    std::vector<char> data;
    NetlinkMessageBuffer() : data(64 * 1024) {}
};

// Utility to set non-blocking
void setNonBlocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags == -1) {
        throw std::runtime_error("fcntl(F_GETFL) failed");
    }
    if (fcntl(fd, F_SETFL, flags | O_NONBLOCK) == -1) {
        throw std::runtime_error("fcntl(F_SETFL) failed");
    }
}

// Attribute parsing helpers
template <typename T, size_t N>
void parseRtAttributes(struct rtattr* rta, int len, T (&attrs)[N]) {
    std::fill(std::begin(attrs), std::end(attrs), nullptr);
    while (RTA_OK(rta, len)) {
        if (rta->rta_type < N) {
            attrs[rta->rta_type] = rta;
        }
        rta = RTA_NEXT(rta, len);
    }
}

class SnapshotCollector {
public:
    std::vector<std::string> collect() {
        openSocket();
        sendGetLinkDump();
        receiveDump();
        sendGetRouteDump(AF_INET);
        receiveDump();
        sendGetRouteDump(AF_INET6);
        receiveDump();
        recomputeManagedInterfaces(false);
        ::close(nlSocket_);
        return namesOfManaged();
    }

private:
    int nlSocket_ = -1;

    std::unordered_map<int, std::string> ifindexToName_;
    std::unordered_set<int> upInterfaces_;
    std::unordered_set<int> defaultRouteIfacesV4_;
    std::unordered_set<int> defaultRouteIfacesV6_;
    std::unordered_set<int> managedIfaces_; // up ∩ (v4默认网关 ∪ v6默认网关)

    void openSocket() {
        nlSocket_ = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
        if (nlSocket_ < 0) {
            throw std::runtime_error("socket(AF_NETLINK) 失败");
        }

        sockaddr_nl addr{};
        addr.nl_family = AF_NETLINK;
        addr.nl_groups = RTMGRP_LINK | RTMGRP_IPV4_ROUTE | RTMGRP_IPV6_ROUTE;

        if (bind(nlSocket_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
            throw std::runtime_error("bind(AF_NETLINK) 失败");
        }

        setNonBlocking(nlSocket_);
    }


    void sendNetlinkRequest(uint16_t nlmsgType, uint16_t flags, uint8_t family) {
        struct {
            nlmsghdr nlh;
            rtgenmsg gen;
        } req{};

        req.nlh.nlmsg_len = sizeof(req);
        req.nlh.nlmsg_type = nlmsgType;
        req.nlh.nlmsg_flags = flags | NLM_F_REQUEST;
        req.nlh.nlmsg_seq = static_cast<uint32_t>(::time(nullptr));
        req.nlh.nlmsg_pid = 0;
        req.gen.rtgen_family = family;

        sockaddr_nl nladdr{};
        nladdr.nl_family = AF_NETLINK;

        struct iovec iov{ &req, sizeof(req) };
        struct msghdr msg{};
        msg.msg_name = &nladdr;
        msg.msg_namelen = sizeof(nladdr);
        msg.msg_iov = &iov;
        msg.msg_iovlen = 1;

        if (sendmsg(nlSocket_, &msg, 0) < 0) {
            throw std::runtime_error("sendmsg 失败");
        }
    }

    void sendGetLinkDump() { sendNetlinkRequest(RTM_GETLINK, NLM_F_DUMP, AF_PACKET); }
    void sendGetRouteDump(int family) { sendNetlinkRequest(RTM_GETROUTE, NLM_F_DUMP, static_cast<uint8_t>(family)); }

    void receiveDump() {
        NetlinkMessageBuffer buffer;
        while (true) {
            sockaddr_nl nladdr{};
            struct iovec iov{ buffer.data.data(), buffer.data.size() };
            struct msghdr msg{};
            msg.msg_name = &nladdr;
            msg.msg_namelen = sizeof(nladdr);
            msg.msg_iov = &iov;
            msg.msg_iovlen = 1;

            ssize_t len = recvmsg(nlSocket_, &msg, 0);
            if (len < 0) {
                if (errno == EINTR) continue;
                if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                throw std::runtime_error("recvmsg 失败");
            }
            if (len == 0) break;

            for (nlmsghdr* hdr = reinterpret_cast<nlmsghdr*>(buffer.data.data());
                 NLMSG_OK(hdr, static_cast<unsigned>(len));
                 hdr = NLMSG_NEXT(hdr, len)) {
                if (hdr->nlmsg_type == NLMSG_DONE) return;
                if (hdr->nlmsg_type == NLMSG_ERROR) {
                    continue;
                }
                dispatchNetlinkMessage(hdr);
            }
        }
    }

    void receiveAndProcess(NetlinkMessageBuffer& buffer) {
        while (true) {
            sockaddr_nl nladdr{};
            struct iovec iov{ buffer.data.data(), buffer.data.size() };
            struct msghdr msg{};
            msg.msg_name = &nladdr;
            msg.msg_namelen = sizeof(nladdr);
            msg.msg_iov = &iov;
            msg.msg_iovlen = 1;

            ssize_t len = recvmsg(nlSocket_, &msg, 0);
            if (len < 0) {
                if (errno == EINTR) continue;
                if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                throw std::runtime_error("recvmsg 失败");
            }
            if (len == 0) break;

            bool seenDone = false;
            for (nlmsghdr* hdr = reinterpret_cast<nlmsghdr*>(buffer.data.data());
                 NLMSG_OK(hdr, static_cast<unsigned>(len));
                 hdr = NLMSG_NEXT(hdr, len)) {
                if (hdr->nlmsg_type == NLMSG_DONE) { seenDone = true; break; }
                if (hdr->nlmsg_type == NLMSG_ERROR) {
                    continue;
                }
                dispatchNetlinkMessage(hdr);
            }
            if (seenDone) break;
        }
        recomputeManagedInterfaces(/*announceChanges=*/true);
    }

    void dispatchNetlinkMessage(struct nlmsghdr* hdr) {
        switch (hdr->nlmsg_type) {
            case RTM_NEWLINK:
            case RTM_DELLINK:
                handleLink(reinterpret_cast<ifinfomsg*>(NLMSG_DATA(hdr)), IFLA_RTA(reinterpret_cast<ifinfomsg*>(NLMSG_DATA(hdr))), IFLA_PAYLOAD(hdr));
                break;
            case RTM_NEWROUTE:
            case RTM_DELROUTE:
                handleRoute(reinterpret_cast<rtmsg*>(NLMSG_DATA(hdr)), RTM_RTA(reinterpret_cast<rtmsg*>(NLMSG_DATA(hdr))), RTM_PAYLOAD(hdr), hdr->nlmsg_type);
                break;
            default:
                break;
        }
    }

    void handleLink(ifinfomsg* info, void* attrHead, int attrLen) {
        struct rtattr* attrs[IFLA_MAX + 1];
        parseRtAttributes(reinterpret_cast<struct rtattr*>(attrHead), attrLen, attrs);

        int ifindex = info->ifi_index;
        std::string ifname;
        if (attrs[IFLA_IFNAME]) {
            char name[IFNAMSIZ]{};
            std::snprintf(name, sizeof(name), "%s", reinterpret_cast<char*>(RTA_DATA(attrs[IFLA_IFNAME])));
            ifname = name;
            ifindexToName_[ifindex] = ifname;
        } else {
            auto it = ifindexToName_.find(ifindex);
            if (it != ifindexToName_.end()) ifname = it->second; // best effort
        }

        bool isLoopback = (info->ifi_flags & IFF_LOOPBACK) != 0;
        bool isUp = (info->ifi_flags & IFF_UP) != 0;

        if (isUp && !isLoopback) {
            upInterfaces_.insert(ifindex);
        } else {
            upInterfaces_.erase(ifindex);
        }

        // 如果是删除链路，把所有相关状态清理
        if (info->ifi_change == ~0U || (info->ifi_flags == 0)) {
            // Heuristic: DELLINK 通常会带来 flags = 0 或者 ifi_change=~0
            defaultRouteIfacesV4_.erase(ifindex);
            defaultRouteIfacesV6_.erase(ifindex);
        }

        (void)ifname; // 保留局部变量以兼容日志需求，但不输出
    }

    static bool isDefaultRoute(const rtmsg* rtm) {
        return rtm->rtm_dst_len == 0 &&
               (rtm->rtm_table == RT_TABLE_MAIN || rtm->rtm_table == RT_TABLE_DEFAULT || rtm->rtm_table == RT_TABLE_UNSPEC) &&
               (rtm->rtm_scope == RT_SCOPE_UNIVERSE || rtm->rtm_scope == RT_SCOPE_NOWHERE || rtm->rtm_scope == RT_SCOPE_SITE);
    }

    void handleRoute(rtmsg* rtm, void* attrHead, int attrLen, int nlmsgType) {
        struct rtattr* attrs[RTA_MAX + 1];
        parseRtAttributes(reinterpret_cast<struct rtattr*>(attrHead), attrLen, attrs);

        if (!isDefaultRoute(rtm)) {
            return;
        }

        int oif = -1;
        bool hasGateway = false;
        if (attrs[RTA_OIF]) {
            oif = *reinterpret_cast<int*>(RTA_DATA(attrs[RTA_OIF]));
        }
        if (attrs[RTA_GATEWAY]) {
            hasGateway = true;
        }

        if (oif <= 0 || !hasGateway) {
            // 没有明确网关或未绑定接口，视为无上网能力
            return;
        }

        auto& targetSet = (rtm->rtm_family == AF_INET) ? defaultRouteIfacesV4_ : defaultRouteIfacesV6_;
        if (nlmsgType == RTM_NEWROUTE) {
            targetSet.insert(oif);
        } else if (nlmsgType == RTM_DELROUTE) {
            targetSet.erase(oif);
        }

        (void)nlmsgType;
        (void)rtm;
    }

    void recomputeManagedInterfaces(bool announceChanges) {
        (void)announceChanges;
        std::unordered_set<int> newManaged;
        for (int ifindex : upInterfaces_) {
            if (defaultRouteIfacesV4_.count(ifindex) || defaultRouteIfacesV6_.count(ifindex)) {
                newManaged.insert(ifindex);
            }
        }
        managedIfaces_.swap(newManaged);
    }

    std::vector<std::string> namesOfManaged() const {
        std::vector<std::string> names;
        names.reserve(managedIfaces_.size());
        for (int idx : managedIfaces_) {
            auto it = ifindexToName_.find(idx);
            if (it != ifindexToName_.end()) names.push_back(it->second);
        }
        std::sort(names.begin(), names.end());
        return names;
    }
};

} // namespace

// Thread-safe lazy singleton members
std::once_flag NetInterfaceManager::s_onceFlag;
std::shared_ptr<NetInterfaceManager> NetInterfaceManager::s_instance;

std::shared_ptr<NetInterfaceManager> NetInterfaceManager::getInstance() {
    std::call_once(s_onceFlag, [](){ s_instance = std::shared_ptr<NetInterfaceManager>(new NetInterfaceManager()); });
    return s_instance;
}

std::vector<std::string> NetInterfaceManager::getInternetInterfaces() {
    SnapshotCollector collector;
    return collector.collect();
}

#else

#include "net_iface.h"
#include <string>
#include <vector>

// Thread-safe lazy singleton members
std::once_flag NetInterfaceManager::s_onceFlag;
std::shared_ptr<NetInterfaceManager> NetInterfaceManager::s_instance;

std::shared_ptr<NetInterfaceManager> NetInterfaceManager::getInstance() {
    std::call_once(s_onceFlag, [](){ s_instance = std::shared_ptr<NetInterfaceManager>(new NetInterfaceManager()); });
    return s_instance;
}

std::vector<std::string> NetInterfaceManager::getInternetInterfaces() {
    return {};
}

#endif // defined(__linux__)
