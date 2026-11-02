#include "net_tcp.h"

#include <sys/socket.h>
#include <unistd.h>
#include <cstring>
#include <string>
#include <algorithm>
#include <vector>
#include <netinet/in.h>
#include <net/if.h>
#include <fstream>
#include <sstream>

#include <linux/netlink.h>
#include <linux/sock_diag.h>
#include <linux/inet_diag.h>
#include <linux/rtnetlink.h>
#include <netinet/tcp.h>

std::once_flag TcpLossMonitor::s_onceFlag;
std::shared_ptr<TcpLossMonitor> TcpLossMonitor::s_instance;

std::shared_ptr<TcpLossMonitor> TcpLossMonitor::getInstance() {
    std::call_once(s_onceFlag, [](){ s_instance = std::shared_ptr<TcpLossMonitor>(new TcpLossMonitor()); });
    return s_instance;
}

// Try to map socket to interface index using iif/oif from inet_diag_msg (idiag_if)
static bool diagDumpFamilyIface(int nlSock, int family, int filterIfindex,
                                uint64_t& segsOutApprox, uint64_t& segsInApprox, uint64_t& totalRetrans) {
    segsOutApprox = segsInApprox = totalRetrans = 0;

    struct {
        nlmsghdr nlh;
        inet_diag_req_v2 req;
    } msg{};

    msg.nlh.nlmsg_len = sizeof(msg);
    msg.nlh.nlmsg_type = SOCK_DIAG_BY_FAMILY;
    msg.nlh.nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP;
    msg.nlh.nlmsg_seq = 1;
    msg.nlh.nlmsg_pid = 0;

    msg.req.sdiag_family = static_cast<uint8_t>(family);
    msg.req.sdiag_protocol = IPPROTO_TCP;
    msg.req.idiag_states = 0xFFFFFFFFu; // all states
    msg.req.idiag_ext = (1 << (INET_DIAG_INFO - 1)); // tcp_info

    sockaddr_nl nladdr{};
    nladdr.nl_family = AF_NETLINK;

    iovec iov{};
    iov.iov_base = &msg;
    iov.iov_len = sizeof(msg);

    msghdr msgh{};
    msgh.msg_name = &nladdr;
    msgh.msg_namelen = sizeof(nladdr);
    msgh.msg_iov = &iov;
    msgh.msg_iovlen = 1;

    if (sendmsg(nlSock, &msgh, 0) < 0) {
        return false;
    }

    std::vector<char> buf(256 * 1024);
    while (true) {
        iovec riov{ buf.data(), buf.size() };
        msghdr rmsg{};
        rmsg.msg_name = &nladdr;
        rmsg.msg_namelen = sizeof(nladdr);
        rmsg.msg_iov = &riov;
        rmsg.msg_iovlen = 1;

        ssize_t len = recvmsg(nlSock, &rmsg, 0);
        if (len < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (len == 0) break;

        for (nlmsghdr* h = reinterpret_cast<nlmsghdr*>(buf.data()); NLMSG_OK(h, (unsigned)len); h = NLMSG_NEXT(h, len)) {
            if (h->nlmsg_type == NLMSG_DONE) return true;
            if (h->nlmsg_type == NLMSG_ERROR) return false;
            if (h->nlmsg_type != SOCK_DIAG_BY_FAMILY) continue;

            inet_diag_msg* im = reinterpret_cast<inet_diag_msg*>(NLMSG_DATA(h));
            // Filter using socket's interface index in sockid
            if (filterIfindex > 0 && im->id.idiag_if != static_cast<uint32_t>(filterIfindex)) {
                continue; // filter by interface index
            }
            int rtalen = h->nlmsg_len - NLMSG_LENGTH(sizeof(*im));
            for (rtattr* attr = (rtattr*)(((char*)im) + NLMSG_ALIGN(sizeof(*im)));
                 RTA_OK(attr, rtalen);
                 attr = RTA_NEXT(attr, rtalen)) {
                if (attr->rta_type == INET_DIAG_INFO) {
                    tcp_info* ti = reinterpret_cast<tcp_info*>(RTA_DATA(attr));
                    totalRetrans += ti->tcpi_total_retrans;
                    // 近似分母：无法使用 tcpi_segs_out 时，采用未确认+已重传+已SACK 估算
                    segsOutApprox += static_cast<uint64_t>(ti->tcpi_unacked)
                                   + static_cast<uint64_t>(ti->tcpi_retrans)
                                   + static_cast<uint64_t>(ti->tcpi_sacked);
                }
            }
        }
    }
    return true;
}

static int ifnameToIndex(const std::string& name) {
    // use standard API
    unsigned ifi = if_nametoindex(name.c_str());
    return ifi == 0 ? -1 : (int)ifi;
}

static bool rtnlGetIfTxPackets(int ifindex, uint64_t& txPackets) {
    txPackets = 0;
    int nl = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (nl < 0) return false;

    struct {
        nlmsghdr nlh;
        ifinfomsg ifm;
    } req{};
    req.nlh.nlmsg_len = sizeof(req);
    req.nlh.nlmsg_type = RTM_GETLINK;
    req.nlh.nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
    req.ifm.ifi_family = AF_UNSPEC;
    req.ifm.ifi_index = ifindex;

    sockaddr_nl nladdr{}; nladdr.nl_family = AF_NETLINK;
    iovec iov{ &req, sizeof(req) };
    msghdr msg{}; msg.msg_name = &nladdr; msg.msg_namelen = sizeof(nladdr); msg.msg_iov = &iov; msg.msg_iovlen = 1;
    if (sendmsg(nl, &msg, 0) < 0) { close(nl); return false; }

    std::vector<char> buf(16 * 1024);
    iov = { buf.data(), buf.size() };
    msghdr rmsg{}; rmsg.msg_name = &nladdr; rmsg.msg_namelen = sizeof(nladdr); rmsg.msg_iov = &iov; rmsg.msg_iovlen = 1;
    ssize_t len = recvmsg(nl, &rmsg, 0);
    close(nl);
    if (len <= 0) return false;

    for (nlmsghdr* h = reinterpret_cast<nlmsghdr*>(buf.data()); NLMSG_OK(h, (unsigned)len); h = NLMSG_NEXT(h, len)) {
        if (h->nlmsg_type == NLMSG_ERROR) return false;
        if (h->nlmsg_type != RTM_NEWLINK) continue;
        ifinfomsg* ifm = reinterpret_cast<ifinfomsg*>(NLMSG_DATA(h));
        if ((int)ifm->ifi_index != ifindex) continue;
        int attrlen = h->nlmsg_len - NLMSG_LENGTH(sizeof(*ifm));
        for (rtattr* attr = IFLA_RTA(ifm); RTA_OK(attr, attrlen); attr = RTA_NEXT(attr, attrlen)) {
            if (attr->rta_type == IFLA_STATS64) {
                struct rtnl_link_stats64 { uint64_t rx_packets, tx_packets, rx_bytes, tx_bytes; };
                if (RTA_PAYLOAD(attr) >= sizeof(rtnl_link_stats64)) {
                    auto* st = reinterpret_cast<const rtnl_link_stats64*>(RTA_DATA(attr));
                    txPackets = st->tx_packets;
                    return true;
                }
            } else if (attr->rta_type == IFLA_STATS) {
                struct rtnl_link_stats { uint32_t rx_packets, tx_packets; };
                if (RTA_PAYLOAD(attr) >= sizeof(rtnl_link_stats)) {
                    auto* st = reinterpret_cast<const rtnl_link_stats*>(RTA_DATA(attr));
                    txPackets = st->tx_packets;
                    return true;
                }
            }
        }
    }
    return false;
}

static bool diagSampleIfaceAll(const std::string& iface, uint64_t& segsOutApprox, uint64_t& segsInApprox, uint64_t& totalRetrans) {
    segsOutApprox = segsInApprox = totalRetrans = 0;
    int ifidx = ifnameToIndex(iface);
    if (ifidx <= 0) return false;

    int nl = socket(AF_NETLINK, SOCK_DGRAM, NETLINK_SOCK_DIAG);
    if (nl < 0) return false;

    uint64_t so4 = 0, si4 = 0, r4 = 0, so6 = 0, si6 = 0, r6 = 0;
    bool ok4 = diagDumpFamilyIface(nl, AF_INET, ifidx, so4, si4, r4);
    bool ok6 = diagDumpFamilyIface(nl, AF_INET6, ifidx, so6, si6, r6);

    close(nl);
    if (!(ok4 || ok6)) return false;
    segsOutApprox = so4 + so6;
    segsInApprox = si4 + si6;
    totalRetrans = r4 + r6;
    // 若近似分母为0，尝试用该接口 L2 层 tx_packets 作为最小可用分母（会包含非TCP）
    if (segsOutApprox == 0) {
        uint64_t txp = 0;
        if (rtnlGetIfTxPackets(ifidx, txp)) segsOutApprox = txp;
    }
    return true;
}

bool TcpLossMonitor::sample(TcpStats& outStats) {
    // system-wide: 纯 netlink 近似统计
    int nl = socket(AF_NETLINK, SOCK_DGRAM, NETLINK_SOCK_DIAG);
    if (nl < 0) return false;
    uint64_t so4 = 0, si4 = 0, r4 = 0, so6 = 0, si6 = 0, r6 = 0;
    bool ok4 = diagDumpFamilyIface(nl, AF_INET, -1, so4, si4, r4);
    bool ok6 = diagDumpFamilyIface(nl, AF_INET6, -1, so6, si6, r6);
    close(nl);
    if (!(ok4 || ok6)) return false;
    outStats.retransSegs = r4 + r6;
    outStats.outSegs = so4 + so6;  // 近似分母
    outStats.inSegs = si4 + si6;
    outStats.valid = true;
    return true;
}

bool TcpLossMonitor::sampleForInterface(const std::string& ifaceName, TcpStats& outStats) {
    uint64_t so = 0, si = 0, r = 0;
    if (!diagSampleIfaceAll(ifaceName, so, si, r)) return false;
    outStats.outSegs = so;
    outStats.inSegs = si;
    outStats.retransSegs = r;
    outStats.valid = true;
    return true;
}

TcpLossResult TcpLossMonitor::compute(const TcpStats& prev,
                                      const TcpStats& curr,
                                      uint64_t minSent,
                                      double degradedThresholdPct,
                                      double poorThresholdPct) {
    TcpLossResult r;
    if (!prev.valid || !curr.valid) { r.level = "insufficient"; return r; }
    if (curr.outSegs < prev.outSegs || curr.retransSegs < prev.retransSegs) {
        r.level = "insufficient"; // counters reset/overflow
        return r;
    }
    uint64_t deltaOut = curr.outSegs - prev.outSegs;
    uint64_t deltaRetrans = curr.retransSegs - prev.retransSegs;
    r.sentDelta = deltaOut;
    r.retransDelta = deltaRetrans;
    if (deltaOut < minSent) {
        r.level = "insufficient";
        return r;
    }
    r.ratePercent = (deltaRetrans * 100.0) / (double)deltaOut;
    if (r.ratePercent >= poorThresholdPct) r.level = "poor";
    else if (r.ratePercent >= degradedThresholdPct) r.level = "degraded";
    else r.level = "good";
    return r;
}
