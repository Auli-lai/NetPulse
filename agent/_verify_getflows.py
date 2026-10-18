"""
GetFlows 的 C++ 侧离线验证 —— 在没有 dbus / libbpf 的机器上也能跑。

    python _verify_getflows.py

它做的事：把 dbus_service.cpp 里**真实的** handleGetFlows 函数原文抽出来，
配上一组功能性的桩（假的 D-Bus、假的 NetTrafficAnalyzer），编译并运行它，
然后检查它吐出来的 JSON 能不能被 json.loads 解析、字段对不对。

为什么要费这个劲：GetFlows 的 C++ 改动没法在本机真正编译（dbus-1 和 libbpf
都是 Linux 专有）。如果不做这层验证，第一个发现编译错误的人会是用户 ——
而他当时正在重新编译整个服务端，报错信息会淹没在一堆输出里。

抽函数原文而不是复制一份，是为了保证验证的就是真正会进编译的那段代码。

验证通过不代表整个服务端能编译（真实的 dbus API 签名可能不同），
但能挡住绝大多数问题：语法、括号、类型、JSON 拼错。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.join(os.path.dirname(_HERE), "server")

_STUBS = r"""
// ---- 桩：只提供 handleGetFlows 用到的那部分接口 ----
#pragma once
#include <cstdint>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <iostream>

// ---- glog / logger 桩 ----
#define LOG(level) std::cerr
namespace weaknet_dbus {
namespace LogModule {
constexpr const char* DBUS = "dbus";
}
}
#define LOG_WARNING(module, msg) std::cerr << "[" << module << "] " << msg << "\n"

// ---- dbus 桩 ----
struct DBusConnection { int dummy = 0; };
struct DBusMessage { std::string reply; bool is_method_return = false; };
struct DBusMessageIter { int dummy = 0; };

enum { DBUS_TYPE_STRING = 115 };

// 捕获回包内容，供最后断言用
extern std::string g_last_reply;
extern bool g_reply_sent;

inline DBusMessage* dbus_message_new_method_return(DBusMessage*) {
    return new DBusMessage();
}
inline void dbus_message_iter_init_append(DBusMessage*, DBusMessageIter*) {}
inline int dbus_message_iter_append_basic(DBusMessageIter*, int, const void* value) {
    g_last_reply = *static_cast<const char* const*>(value);
    return 1;
}
inline int dbus_connection_send(DBusConnection*, DBusMessage* m, void*) {
    m->reply = g_last_reply;
    g_reply_sent = true;
    return 1;
}
inline void dbus_connection_flush(DBusConnection*) {}
inline void dbus_message_unref(DBusMessage* m) { delete m; }

// ---- NetTrafficAnalyzer 桩 ----
struct FlowRate {
    std::string src;
    std::string dst;
    int sport = 0;
    int dport = 0;
    std::string proto;
    uint64_t bps = 0;
    uint64_t pps = 0;
    uint32_t pid = 0;
};

extern bool g_should_throw;

class NetTrafficAnalyzer {
public:
    static std::shared_ptr<NetTrafficAnalyzer> getInstance() {
        static std::shared_ptr<NetTrafficAnalyzer> inst(new NetTrafficAnalyzer());
        return inst;
    }
    std::vector<FlowRate> sampleTopFlows(int, int) {
        if (g_should_throw) throw std::runtime_error("模拟采集失败");
        std::vector<FlowRate> out;
        FlowRate a;
        a.src = "10.0.0.5"; a.dst = "223.5.5.5"; a.sport = 51422; a.dport = 443;
        a.proto = "TCP"; a.bps = 2700000; a.pps = 1480; a.pid = 8891;
        out.push_back(a);
        FlowRate b;
        b.src = "10.0.0.5"; b.dst = "140.82.114.4"; b.sport = 51430; b.dport = 443;
        b.proto = "UDP"; b.bps = 0; b.pps = 0; b.pid = 0;   // 边界：全零值
        out.push_back(b);
        return out;
    }
    const std::string& boundInterface() const { return bound_; }
private:
    std::string bound_ = "eth0";
};

// ---- DbusService 桩：只声明，不定义其它成员 ----
namespace weaknet_dbus {
class DbusService {
public:
    bool handleGetFlows(DBusConnection* conn, DBusMessage* msg);
};
}
"""


def _extract_function(src_text: str, signature: str) -> str:
    """从源文件里原样抽出函数体（从签名行到与之配对的收尾大括号）。"""
    start = src_text.index(signature)
    i = src_text.index("{", start)
    depth = 0
    for j in range(i, len(src_text)):
        if src_text[j] == "{":
            depth += 1
        elif src_text[j] == "}":
            depth -= 1
            if depth == 0:
                return src_text[start : j + 1]
    raise ValueError(f"找不到 {signature} 的结尾")


def main() -> int:
    dbus_cpp = os.path.join(_SERVER, "src", "dbus_service.cpp")
    if not os.path.exists(dbus_cpp):
        print(f"找不到 {dbus_cpp}")
        return 2

    src = open(dbus_cpp, encoding="utf-8").read()

    try:
        func = _extract_function(src, "bool DbusService::handleGetFlows")
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1

    print(f"已从 dbus_service.cpp 抽出 handleGetFlows：{len(func.splitlines())} 行\n")

    # 编译单元：桩 + 真实函数 + 一个 main 把结果打出来
    unit = "\n".join([
        _STUBS,
        "std::string g_last_reply;",
        "bool g_reply_sent = false;",
        "bool g_should_throw = false;",
        "namespace weaknet_dbus {",
        func,
        "}  // namespace weaknet_dbus",
        "int main(int argc, char**) {",
        "    g_should_throw = (argc > 1);",
        "    DBusConnection conn;",
        "    DBusMessage msg;",
        "    weaknet_dbus::DbusService svc;",
        "    svc.handleGetFlows(&conn, &msg);",
        "    if (!g_reply_sent) { std::cout << \"__NO_REPLY__\"; return 3; }",
        "    // 注意打的是 g_last_reply 而不是 msg.reply —— 真实的",
        "    // dbus_message_new_method_return 会**新建**一条消息来回复，",
        "    // 原始的 msg 上不会挂回复内容。",
        "    std::cout << g_last_reply;",
        "    return 0;",
        "}",
    ])

    tmp = tempfile.mkdtemp(prefix="getflows_check_")
    try:
        cpp = os.path.join(tmp, "check.cpp")
        exe = os.path.join(tmp, "check.exe")
        with open(cpp, "w", encoding="utf-8") as f:
            f.write(unit)

        print("① 语法检查（g++ -fsyntax-only）…")
        r = subprocess.run(
            ["g++", "-std=c++17", "-fsyntax-only", cpp],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print("❌ 编译失败：")
            print(r.stderr[:3000])
            return 1
        print("   ✅ 通过\n")

        print("② 运行并检查输出 JSON…")
        r = subprocess.run(["g++", "-std=c++17", "-o", exe, cpp],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("❌ 链接失败：")
            print(r.stderr[:3000])
            return 1

        ok = True

        r = subprocess.run([exe], capture_output=True, text=True)
        payload = r.stdout.strip()
        if r.returncode != 0:
            print(f"❌ 运行失败（退出码 {r.returncode}）：{payload[:300]}")
            return 1

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            print(f"❌ 输出的不是合法 JSON：{exc}")
            print(f"   原文：{payload[:500]}")
            return 1

        print(f"   ✅ JSON 合法：{json.dumps(data, ensure_ascii=False)[:200]}…\n")

        # ---- 断言字段 ----
        checks = [
            ("顶层有 flows 数组", isinstance(data.get("flows"), list)),
            ("顶层有 interface", data.get("interface") == "eth0"),
            ("顶层有 interval_seconds", data.get("interval_seconds") == 1),
            ("flows 有 2 条", len(data.get("flows", [])) == 2),
        ]
        first = (data.get("flows") or [{}])[0]
        for field in ("src", "dst", "sport", "dport", "protocol", "bps", "pps", "pid"):
            checks.append((f"每条 flow 有 {field}", field in first))
        checks.append(("src 是 IP 字符串", first.get("src") == "10.0.0.5"))
        checks.append(("sport 是数字", first.get("sport") == 51422))
        checks.append(("bps 是数字", first.get("bps") == 2700000))
        checks.append(("protocol 是 TCP", first.get("protocol") == "TCP"))

        second = (data.get("flows") or [{}, {}])[1]
        checks.append(("第二条 protocol 是 UDP", second.get("protocol") == "UDP"))
        checks.append(("全零值不会被漏掉", second.get("bps") == 0))

        for name, passed in checks:
            print(f"   {'✅' if passed else '❌'} {name}")
            ok = ok and passed

        # ---- 异常路径：采集抛异常时必须回合法 JSON，不能挂 ----
        print("\n③ 异常路径：采集抛异常时的行为…")
        r = subprocess.run([exe, "--throw"], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"   ❌ 异常没被兜住，进程退出了（码 {r.returncode}）")
            ok = False
        else:
            payload = r.stdout.strip()
            try:
                data = json.loads(payload)
                has_err = "error" in data and data.get("flows") == []
                print(f"   {'✅' if has_err else '❌'} 回的是合法 JSON 且带 error：{payload[:120]}")
                ok = ok and has_err
            except json.JSONDecodeError:
                print(f"   ❌ 异常路径回的不是合法 JSON：{payload[:200]}")
                ok = False

        print()
        print("=" * 66)
        if ok:
            print("✅ 全部通过 —— handleGetFlows 的语法、JSON 结构、异常兜底都验证过了")
            print()
            print("⚠️  注意：这验证的是**函数本身**。真实编译还需要 Linux 上的")
            print("   dbus-1 / libbpf 头文件，那部分在本机验证不了。")
            return 0
        print("❌ 有检查项失败")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # console.py 在隔壁 rag/，本脚本没有 import tools.py，所以要自己挂路径
    sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "rag"))
    from console import enable_utf8_output

    enable_utf8_output()
    sys.exit(main())
