#!/usr/bin/env bash
#
# 容器内启动脚本：同一个 root 用户起 session bus，服务端和 Web 共用一条总线，
# 就不会出现"root 连不上会话总线"的问题。
#
# 容器内用 root 跑可接受——这是本地诊断镜像，端口只绑 127.0.0.1；
# 想把攻击面再收一点，就换成非 root 用户 + setcap（libcap-dev 已在镜像里）。
set -uo pipefail

# 没有 machine-id 时 dbus-daemon 起会话总线会告警，先确保一个
mkdir -p /var/lib/dbus
dbus-uuidgen > /var/lib/dbus/machine-id 2>/dev/null || true

# 起一条会话总线（root 自己的，服务端和 Web 同用户同总线）
ADDR="$(dbus-daemon --session --fork --print-address)"
export DBUS_SESSION_BUS_ADDRESS="$ADDR"

# 检索索引缺失时，用离线伪向量先建一个，保证 RAG 开箱即用（不调 embedding API）
if [ ! -f /app/rag/index_store/chunks.json ]; then
  echo "[entrypoint] 建离线检索索引（RAG_OFFLINE=1）…"
  ( cd /app/rag && PYTHONPATH=/app RAG_OFFLINE=1 python3 build_index.py --sample ) \
    || echo "[entrypoint] 建索引失败，Web 演示/知识库检索会降级（不影响启动）"
fi

# 起 C++ 服务端（后台；binary 用相对路径 ../build/flow_rate.bpf.o 找 eBPF 对象，所以 cd 到 bin）
cd /app/server/bin
./weaknet-dbus-server &
SRV_PID=$!

# 起 Web（前台，容器生命周期跟着它走；退出时一起收掉服务端）
cd /app/web
trap 'kill $SRV_PID 2>/dev/null' EXIT INT TERM
exec uvicorn app:app --host 0.0.0.0 --port 8000
