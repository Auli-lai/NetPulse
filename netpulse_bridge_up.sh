#!/usr/bin/env bash
# ===========================================================================
# NetPulse 一键拉起宿主桥接（daemon 重启 + 容器保活 + 端到端验证）
# ---------------------------------------------------------------------------
# 解决"容器看不到宿主 WiFi"的运行时部分：
#   1) 杀掉旧 daemon，用【最新代码】重新起（常驻后台）
#   2) 等首次采集完成（最多 20s）
#   3) 宿主侧 curl 验证
#   4) 确保容器 netpulse-full 在跑，并跑容器内合并诊断，打印 PASS/FAIL
#
# 用法：
#   bash netpulse_bridge_up.sh
#
# 注意：daemon 跑在宿主，本脚本直接读项目里的 agent/host_bridge_daemon.py，
#       所以改完代码后只要跑这一条就能用上新逻辑，无需重建镜像。
# ===========================================================================
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_PORT="${NETPULSE_BRIDGE_PORT:-18900}"
CONTAINER="${NETPULSE_CONTAINER:-netpulse-full}"

echo "== [1/4] 重启宿主桥接守护进程（用最新代码，常驻后台）=="
pkill -f host_bridge_daemon.py 2>/dev/null || true
sleep 1
NETPULSE_FORCE_BRIDGE=1 PYTHONPATH="$ROOT_DIR" \
  nohup python3 "$ROOT_DIR/agent/host_bridge_daemon.py" "$DAEMON_PORT" \
  > /tmp/netpulse-bridge.log 2>&1 &
disown 2>/dev/null || true

echo "== [2/4] 等 daemon 首次采集完成（最多 20s）=="
for _ in $(seq 1 20); do
  if curl -s -o /dev/null "http://127.0.0.1:$DAEMON_PORT/healthz" 2>/dev/null; then
    echo "daemon 就绪"; break
  fi
  sleep 1
done

echo "== [3/4] 宿主侧验证 =="
HOST_JSON="$(curl -sS --max-time 10 "http://127.0.0.1:$DAEMON_PORT/host-net" || true)"
echo "${HOST_JSON:-<空，daemon 未响应>}" | head -c 400; echo

echo "== [4/4] 容器侧验证 =="
if ! docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  echo "容器 $CONTAINER 不存在，请先跑一次：./start.sh --docker-full"
  exit 1
fi
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
  echo "容器未运行，docker start $CONTAINER ..."
  docker start "$CONTAINER" >/dev/null
  sleep 5
fi

docker exec -i "$CONTAINER" python3 - <<'PY'
import sys
sys.path.insert(0, '/app/agent')
import netpulse_client as nc
m = nc._merge_host_wifi({"interface": "eth0"})
w = m.get("wifi") or {}
ok = w.get("ok")
print("wifi.ok =", ok, "| ssid =", w.get("ssid"), "| rssi =", w.get("rssi_dbm"))
print("wifi_bridge_error =", m.get("wifi_bridge_error"))
print("RESULT:", "PASS ✅ 桥接打通，浏览器 Agent 应已能看到宿主 WiFi" if ok else "FAIL ❌ 看上面的 error")
PY
