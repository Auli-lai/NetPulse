#!/usr/bin/env bash
# NetPulse 全栈容器入口：总线 → C++ 服务端 → Web，一个进程树全搞定。
#
# 设计要点：
#   · 容器里没有 systemd，session 总线必须自己起（dbus-daemon --fork --print-address）
#   · 服务端起不来（比如内核不让挂 eBPF）**不拖垮 Web** —— 容器照常提供服务，
#     只是实时指标类工具报错、模型绕开，检索/记忆/演示模式全不受影响
#   · 最后 exec uvicorn，让 Web 成为 PID 1 的信号接收者（docker stop 才干净）
set -uo pipefail

cd /app

# ---- 1. session 总线 -------------------------------------------------------
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    ADDR="$(dbus-daemon --session --fork --print-address 2>/dev/null | head -n1)"
    if [ -n "$ADDR" ]; then
        export DBUS_SESSION_BUS_ADDRESS="$ADDR"
        echo "[entrypoint] session bus: $ADDR"
    else
        echo "[entrypoint] ⚠ dbus-daemon 起不来，服务端将不可用（Web 继续跑）"
    fi
fi

# ---- 2. C++ 服务端（eBPF 附不上时它自己降级，不会退出）---------------------
SERVER_PID=""
if [ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -x /app/server/bin/weaknet-dbus-server ]; then
    # cd 到 bin 目录：代码里 eBPF 对象路径是相对的 "../build/flow_rate.bpf.o"
    ( cd /app/server/bin && exec /app/server/bin/weaknet-dbus-server ) &
    SERVER_PID=$!
    echo "[entrypoint] 服务端已拉起（pid=$SERVER_PID），等待注册总线名…"

    # ---- 3. 等 com.example.WeakNet 上线（最多 20s）-------------------------
    for _ in $(seq 1 20); do
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "[entrypoint] ⚠ 服务端退出了 —— 大概率是内核不允许 eBPF。"
            echo "                 Web 以降级模式继续（实时工具不可用）。"
            SERVER_PID=""
            break
        fi
        if dbus-send --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus \
               org.freedesktop.DBus.NameHasOwner string:com.example.WeakNet 2>/dev/null \
               | grep -q "boolean true"; then
            echo "[entrypoint] 服务端已就绪（com.example.WeakNet）"
            break
        fi
        sleep 1
    done
else
    echo "[entrypoint] ⚠ 跳过服务端（缺二进制或没有总线）—— Web 以降级模式继续"
fi

# ---- 4. Web 前台运行（PID 1 收信号）---------------------------------------
cd /app/web
echo "[entrypoint] 启动 Web → http://0.0.0.0:8000"
exec python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
