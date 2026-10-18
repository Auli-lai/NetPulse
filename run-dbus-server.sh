#!/usr/bin/env bash
#
# 启动 NetPulse 服务端 —— 以**你自己的身份**，不需要 sudo。
#
#     ./run-dbus-server.sh
#
# ===========================================================================
# 为什么不再用 sudo 起服务端
# ===========================================================================
#
# 服务端读 eBPF 需要几个 Linux capability（cap_bpf 等），老做法是
# `sudo ./weaknet-dbus-server`。但 sudo 会挖一个死结：
#
#   · 客户端（Python/web）以你的身份跑，挂在**你的** session bus 上
#   · sudo 让服务端变成 root；root 连你的 session bus 会被 D-Bus 安全策略
#     拒绝——而拒绝的表现是"不回复"，libdbus 于是报一句很有误导性的
#     "Did not receive a reply"（听着像超时/网络问题，其实是被拒了）
#
# 正确解法：只给服务端**它需要的那几个 capability**（setcap），然后以你自己
# 的身份跑。两边同用户、同总线，这个问题一次性消失，也不用再跟 sudo -E
# （它在很多 sudoers 配置下会被静默忽略）较劲。
#
# ===========================================================================
# setcap 在 /mnt/e 上会失败——本脚本的自动降级
# ===========================================================================
#
# setcap 靠文件系统的扩展属性（xattr）存能力。/mnt/e 是 Windows 挂载盘
# （drvfs），**不支持**，会报 "Operation not supported"。
#
# 所以本脚本按这个顺序走：
#   1. 二进制已经有能力了？               → 原地启动
#   2. 试着原地 setcap 一下               → 成功就原地启动
#      （项目在 WSL 自己的文件系统里时就能成）
#   3. 原地 setcap 失败（基本就是 drvfs） → 把 server/bin 和 server/build
#      拷到 ~/.netpulse-server（ext4，支持 xattr），在那里 setcap，跑那份拷贝
#
# 全程只有 setcap 那一句需要 sudo，服务端本身始终是你的身份。
#
# 另外，启动前会 cd 到二进制所在目录：代码里 eBPF 对象路径写的是相对路径
# "../build/flow_rate.bpf.o"，只有 cwd = .../server/bin 时才能解析到
# .../server/build/flow_rate.bpf.o。（老做法从项目根目录启动，这个路径其实
# 一直指到项目外面去了，eBPF 静默降级——现在 cd 一下就好。）
#
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDR_FILE="$ROOT_DIR/.dbus-address"
SERVER="$ROOT_DIR/server/bin/weaknet-dbus-server"
BPF_OBJ="$ROOT_DIR/server/build/flow_rate.bpf.o"
CAPS="cap_bpf,cap_perfmon,cap_net_admin,cap_net_raw,cap_sys_admin+eip"
STAGE_DIR="${HOME}/.netpulse-server"
BUS_NAME="com.example.WeakNet"

die() { echo "错误：$*" >&2; exit 1; }
note() { echo "      $*"; }

command -v dbus-send >/dev/null 2>&1 \
    || die "没装 dbus-send（sudo apt install dbus）"

# ---------------------------------------------------------------------------
# 0. 如果是 root 跑进来的，切回普通用户（root 连不上你的 session bus）
# ---------------------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        USER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
        [ -n "$USER_HOME" ] || USER_HOME="/home/$SUDO_USER"
        echo "[0] 检测到 root，切换回普通用户 ${SUDO_USER} 再跑…"
        if command -v runuser >/dev/null 2>&1; then
            exec runuser -u "$SUDO_USER" -- env HOME="$USER_HOME" "$0" "$@"
        fi
        exec sudo -u "$SUDO_USER" -H "$0" "$@"
    fi
    die "请用普通用户跑（不要 sudo ./run-dbus-server.sh）。
     root 连不上你的 session bus，这是 D-Bus 安全策略故意的。"
fi

# ---------------------------------------------------------------------------
# 1. 找到（或起一个）当前用户能用的总线
# ---------------------------------------------------------------------------
reply_ok() {   # $1 = bus 地址。以当前用户身份试连，真能拿到回复才返回 0
    DBUS_SESSION_BUS_ADDRESS="$1" dbus-send --print-reply \
        --dest=org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus.ListNames >/dev/null 2>&1
}

if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -f "$ADDR_FILE" ]; then
    export DBUS_SESSION_BUS_ADDRESS="$(cat "$ADDR_FILE")"
fi

if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] && reply_ok "$DBUS_SESSION_BUS_ADDRESS"; then
    echo "[1/3] 当前用户的 session bus 可用"
    note "$DBUS_SESSION_BUS_ADDRESS"
else
    echo "[1/3] 当前用户没有可用的 session bus，起一个专用的…"
    command -v dbus-daemon >/dev/null 2>&1 \
        || die "没装 dbus-daemon。装一下：sudo apt install dbus"
    ADDR="$(dbus-daemon --session --fork --print-address 2>/dev/null)"
    [ -n "$ADDR" ] || die "dbus-daemon 没能启动（没拿到地址）"
    export DBUS_SESSION_BUS_ADDRESS="$ADDR"
    printf '%s' "$ADDR" > "$ADDR_FILE"
    reply_ok "$ADDR" || die "总线起来了但不应答：$ADDR"
    echo "      已启动：$ADDR"
fi

ADDR="$DBUS_SESSION_BUS_ADDRESS"

# ---------------------------------------------------------------------------
# 2. 准备好能力，让服务端能以当前用户身份跑
# ---------------------------------------------------------------------------
[ -x "$SERVER" ] || die "没找到可执行文件 $SERVER
     先在项目根目录跑一次 make（在 WSL 里跑，不是在 Windows 里）。
     刚改过 server/ 下的代码的话，必须重新 make，否则跑的还是旧二进制。"

# 顺手提醒：源码比二进制新 → 大概率忘了 make
if [ -n "$(find "$ROOT_DIR/server/src" "$ROOT_DIR/server/include" \
              -type f -newer "$SERVER" 2>/dev/null | head -n 1)" ]; then
    note "⚠️  server/ 下有源码比二进制新 —— 改过代码的话先 make 再启动"
fi

file_has_caps() {   # $1 = 文件路径，带着 cap_bpf 返回 0
    command -v getcap >/dev/null 2>&1 || return 1
    getcap "$1" 2>/dev/null | grep -q "cap_bpf"
}

setcap_or_show() {  # $1 = 文件路径；成功返回 0，失败会把 setcap 的输出打出来
    local out rc
    out="$(sudo setcap "$CAPS" "$1" 2>&1)"; rc=$?
    if [ $rc -ne 0 ] && [ -n "$out" ]; then
        note "setcap 输出：$out"
    fi
    return $rc
}

RUN_SERVER="$SERVER"

if file_has_caps "$SERVER"; then
    echo "[2/3] 二进制已带所需能力，原地启动（不需要 sudo）"
else
    echo "[2/3] 给二进制设置 capability（只给需要的几个，从而免 sudo 跑服务端）"
    if setcap_or_show "$SERVER"; then
        echo "      setcap 成功：$SERVER"
    else
        echo "      原地 setcap 失败 —— 大概率是文件系统不支持扩展属性"
        echo "      （/mnt/e 这类 Windows 挂载盘就这样：Operation not supported）"
        echo "      自动降级：把服务端拷到 WSL 自己的文件系统，在那里设能力…"

        if [ ! -f "$BPF_OBJ" ]; then
            note "⚠️  没找到 $BPF_OBJ（eBPF 流量监控会降级，不影响 WiFi 桥接）"
        fi

        rm -rf "$STAGE_DIR"
        mkdir -p "$STAGE_DIR/bin" "$STAGE_DIR/build" || die "建不了 $STAGE_DIR"
        cp "$SERVER" "$STAGE_DIR/bin/" || die "拷贝二进制到 $STAGE_DIR 失败"
        if [ -f "$BPF_OBJ" ]; then cp "$BPF_OBJ" "$STAGE_DIR/build/"; fi
        chmod 0755 "$STAGE_DIR/bin/weaknet-dbus-server"

        STAGE_SERVER="$STAGE_DIR/bin/weaknet-dbus-server"
        setcap_or_show "$STAGE_SERVER" \
            || die "在 $STAGE_DIR 里 setcap 也失败了。手动确认 sudo 可用：
             sudo -v
             sudo setcap $CAPS $STAGE_SERVER"
        RUN_SERVER="$STAGE_SERVER"
        echo "      已就绪：$STAGE_SERVER"
    fi
fi

# ---------------------------------------------------------------------------
# 3. 启动（始终以当前用户身份，和客户端天然同一个总线）
# ---------------------------------------------------------------------------
# cd 到二进制所在目录，让相对路径 "../build/flow_rate.bpf.o" 解析到
# server/build/flow_rate.bpf.o（从项目根目录启动的话它会指到项目外面去）。
cd "$(dirname "$RUN_SERVER")" || die "进不去 $(dirname "$RUN_SERVER")"

if DBUS_SESSION_BUS_ADDRESS="$ADDR" dbus-send --print-reply \
       --dest=org.freedesktop.DBus /org/freedesktop/DBus \
       org.freedesktop.DBus.NameHasOwner string:"$BUS_NAME" 2>/dev/null \
       | grep -q "boolean true"; then
    echo "      ⚠️  总线上已经有一个服务端在跑（$BUS_NAME）。"
    echo "          旧实例可能还占着资源；建议先把它停掉（旧终端 Ctrl-C）。"
fi

echo
echo "[3/3] 启动服务端（身份：$(id -un)，不需要 sudo）"
echo
echo "  ┌──────────────────────────────────────────────────────────────┐"
echo "  │ 客户端要连同一条总线：**新开一个终端**跑下面几行              │"
echo "  └──────────────────────────────────────────────────────────────┘"
echo
echo "      source ~/netpulse-venv/bin/activate"
echo "      export DBUS_SESSION_BUS_ADDRESS='$ADDR'"
echo "      cd \"$ROOT_DIR/web\" && python -m uvicorn app:app --host 0.0.0.0 --port 8000"
echo
echo "  然后浏览器打开 http://localhost:8000"
echo "  接口列表里应该能看到 wlan0 —— 那是你 Windows 的 WiFi，由桥接注入"
echo "  （WSL 里你上网走的就是它，eth0 只是 WSL 的虚拟网卡）"
echo
echo "  停服务端：在本终端按 Ctrl-C"
echo

exec "$RUN_SERVER"
