#!/usr/bin/env bash
#
# 启动 NetPulse 服务端，并保证它也挂在一个"客户端看得见"的 D-Bus 总线上。
#
#     ./run-dbus-server.sh
#
# ===========================================================================
# 为什么需要这个脚本
# ===========================================================================
#
# 服务端读 eBPF 需要 root，而客户端（Python）和服务端**必须挂在同一个
# session bus 上**。这两件事天然冲突：
#
#     sudo ./weaknet-dbus-server      → 连到 root 自己的总线，你这边看不见
#     sudo -E ./weaknet-dbus-server   → 想保留地址，但**不一定生效**（见下）
#
# `sudo -E` 的坑：它是否真的保留环境变量，取决于 sudoers 的配置。
# 默认 `env_reset` 是开的，`-E` 只在 sudoers 里给了 SETENV 标签、
# 或那个变量在 env_keep 白名单里时才起作用。**很多系统上它是被静默忽略的。**
#
# 而没有 DBUS_SESSION_BUS_ADDRESS 时，libdbus 会退回 **autolaunch**
# （通过 X11 属性找总线）。WSL 里通常没有 X session，于是超时，报出这句
# 很有误导性的错：
#
#     连接总线失败: Did not receive a reply. Possible causes include:
#     ... the reply timeout expired, or the network connection was broken
#
# 「没有答复」听起来像网络问题，实际是「压根没人可问」。
#
# ===========================================================================
# 还有第二层坑：root 可能连不上**用户**的 session bus
# ===========================================================================
#
# 就算地址传对了，`/run/user/1000/bus` 是**用户 zzp 的总线**。
# root 连过去时：
#   · 文件权限通常能过（root 有 CAP_DAC_OVERRIDE）
#   · 但 D-Bus 会做 EXTERNAL 认证，把连接方的 UID（0）报给 dbus-daemon
#   · dbus-daemon 的安全策略**可以拒绝**这个连接，而它的表现就是
#     "Did not receive a reply"（不回你，而不是明确说拒绝）
#
# 所以本脚本在启动前会**先用 root 身份试连一次**。
# 连不上就停下来说清楚，而不是让 libdbus 抛一句让人查错方向的报错。
#
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDR_FILE="$ROOT_DIR/.dbus-address"
SERVER="$ROOT_DIR/server/bin/weaknet-dbus-server"

die() { echo "错误：$*" >&2; exit 1; }

[ -x "$SERVER" ] || die "没找到可执行文件 $SERVER
     先在项目根目录跑一次 make"

# ---------------------------------------------------------------------------
# 以某个身份试连总线。返回 0 表示真能拿到回复（不只是 socket 存在）
# ---------------------------------------------------------------------------
reply_as() {   # $1 = "" 表示当前用户，"root" 表示用 sudo
    local as="$1" addr="$2"
    if [ -z "$as" ]; then
        DBUS_SESSION_BUS_ADDRESS="$addr" dbus-send --print-reply \
            --dest=org.freedesktop.DBus /org/freedesktop/DBus \
            org.freedesktop.DBus.ListNames >/dev/null 2>&1
    else
        sudo DBUS_SESSION_BUS_ADDRESS="$addr" dbus-send --print-reply \
            --dest=org.freedesktop.DBus /org/freedesktop/DBus \
            org.freedesktop.DBus.ListNames >/dev/null 2>&1
    fi
}

# ---------------------------------------------------------------------------
# 1. 找到（或起一个）当前用户能用的总线
# ---------------------------------------------------------------------------
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -f "$ADDR_FILE" ]; then
    export DBUS_SESSION_BUS_ADDRESS="$(cat "$ADDR_FILE")"
fi

if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] && reply_as "" "$DBUS_SESSION_BUS_ADDRESS"; then
    echo "[1/3] 当前用户的 session bus 可用"
    echo "      $DBUS_SESSION_BUS_ADDRESS"
else
    echo "[1/3] 当前用户没有可用的 session bus，起一个专用的…"
    command -v dbus-daemon >/dev/null 2>&1 \
        || die "没装 dbus-daemon。装一下：sudo apt install dbus"
    ADDR="$(dbus-daemon --session --fork --print-address 2>/dev/null)"
    [ -n "$ADDR" ] || die "dbus-daemon 没能启动（没拿到地址）"
    export DBUS_SESSION_BUS_ADDRESS="$ADDR"
    printf '%s' "$ADDR" > "$ADDR_FILE"
    reply_as "" "$ADDR" || die "总线起来了但不应答：$ADDR"
    echo "      已启动：$ADDR"
fi

ADDR="$DBUS_SESSION_BUS_ADDRESS"

# ---------------------------------------------------------------------------
# 2. 关键一步：**root 能不能也连上这个总线**
# ---------------------------------------------------------------------------
echo "[2/3] 验证 root 能否连上同一个总线（eBPF 需要 root，这一步过不了就白搭）"

if [ "$(id -u)" -eq 0 ]; then
    echo "      当前已经是 root，跳过"
    exec "$SERVER"
fi

if reply_as "root" "$ADDR"; then
    echo "      可以 —— root 能拿到回复"
else
    cat <<EOF

      ❌ root 连不上这个总线：$ADDR

      这是 **D-Bus 的安全策略在拒绝 root**，不是网络问题。
      dbus-daemon 对拒绝的连接就是"不回复"，所以 libdbus 报的是
      "Did not receive a reply" —— 听起来像超时，实际是被拒了。

      两个解法，推荐第一个：

      ── 解法 A：别用 sudo，改用 setcap（推荐）────────────────────
      这个需求的本质是"要几个 Linux capability"，而 sudo 给的是全部 root。
      只给需要的那几个，服务端就能以**你自己的身份**跑，
      和 Python 客户端天然在同一个总线上，这类问题一次性消失：

          sudo setcap cap_bpf,cap_perfmon,cap_net_admin,cap_net_raw,cap_sys_admin+eip \\
               "$SERVER"
          "$SERVER"          # 不带 sudo

      ⚠️ 但 setcap 依赖文件系统的扩展属性，**在 /mnt/e 这种 Windows 挂载盘上
         通常不支持**（会报 Operation not supported）。

      ── 解法 B：把项目移到 WSL 自己的文件系统 ────────────────────
      如果上面那步报 "Operation not supported"，就是这个问题。
      把项目复制到 WSL 文件系统里（顺带构建和 IO 都会快很多）：

          cp -r "$ROOT_DIR" ~/netpulse
          cd ~/netpulse && make clean && make
          sudo setcap cap_bpf,cap_perfmon,cap_net_admin,cap_net_raw,cap_sys_admin+eip \\
               ./server/bin/weaknet-dbus-server
          ./run-dbus-server.sh

      ── 解法 C：起一个 root 也认的总线（将就，不推荐）────────────
      用 root 起总线，双方都连它。但策略问题可能原样复现，而且要处理
      socket 权限，不比自己跑干净：

          sudo mkdir -p /tmp/netpulse-bus && sudo chmod 777 /tmp/netpulse-bus
          sudo dbus-daemon --session --fork \\
               --address=unix:path=/tmp/netpulse-bus/bus --print-address
          sudo chmod 777 /tmp/netpulse-bus/bus
          # 然后客户端 export DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/netpulse-bus/bus

EOF
    exit 1
fi

# ---------------------------------------------------------------------------
# 3. 启动
# ---------------------------------------------------------------------------
echo "[3/3] 启动服务端"
echo
echo "  ┌──────────────────────────────────────────────────────────────┐"
echo "  │ 客户端要用的地址：**新开一个终端**跑下面这行                  │"
echo "  └──────────────────────────────────────────────────────────────┘"
echo
echo "      export DBUS_SESSION_BUS_ADDRESS='$ADDR'"
echo
echo "  然后验证："
echo "      cd agent && python3 test_tools.py"
echo

# 显式传地址，不用 `sudo -E`（它在很多 sudoers 配置下静默失效）
exec sudo DBUS_SESSION_BUS_ADDRESS="$ADDR" "$SERVER"
