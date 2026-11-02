#!/usr/bin/env bash
#
# NetPulse 一键启动脚本
# ---------------------------------------------------------------------------
# 把"手动装依赖 + 手动编译 + 手动起服务"压成一条命令。
#
# 用法：
#   ./start.sh                自动选最省事的路（有 Docker 走 Docker，否则本地 Python）起 Web
#   ./start.sh --docker       强制 Docker 起 Web（宿主端零依赖，推荐大多数人）
#   ./start.sh --docker-web   同 --docker：只跑 Web（演示 / 体验，无需编译）
#   ./start.sh --docker-full  全栈 Docker：编译 + eBPF + D-Bus + Web，并在宿主后台
#                            起桥接守护进程，让容器能拿到宿主真实上网网卡（WiFi/以太网）
#   ./start.sh --local        强制本地 Python 起 Web（自动建 .venv 并装依赖）
#   ./start.sh --full         完整采集：编译 C++ + 起 D-Bus + 起 Web（需 Linux/WSL2 + 编译工具链）
#   ./start.sh --check        只做环境检查，不启动
#   ./start.sh --help         看这份说明
#
# 说明：
#   · 体验 / Web 模式不需要 Linux、不需要 C++、不需要 API Key（Web 有演示模式）。
#   · eBPF 真实内核采集只在 Linux/WSL2 上可行，--full 会先检查环境。
#   · 容器内看不到宿主真实上网网卡（网络命名空间隔离）。--docker-full 会在宿主后台
#     自动起 agent/host_bridge_daemon.py，把宿主 WiFi / 以太网数据经
#     host.docker.internal 喂给容器；退出时自动回收守护进程。
#   · 脚本不碰系统目录；--full 里 setcap 那一小句会向你要一次 sudo。
# ---------------------------------------------------------------------------

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT_DIR/.venv"
WEB_PORT=8000

info() { echo -e "\033[32m[ok]\033[0m $*"; }
warn() { echo -e "\033[33m[!!]\033[0m $*"; }
die()  { echo -e "\033[31m[xx]\033[0m $*" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

usage() {
  echo "NetPulse 一键启动脚本"
  echo "   ./start.sh                自动选最省事的路（有 Docker 走 Docker，否则本地 Python）起 Web"
  echo "   ./start.sh --docker       强制 Docker 起 Web（宿主端零依赖，推荐大多数人）"
  echo "   ./start.sh --docker-web   同 --docker：只跑 Web（演示 / 体验，无需编译）"
  echo "   ./start.sh --docker-full  全栈 Docker（编译 + eBPF + D-Bus + Web），并在宿主后台"
  echo "                             起桥接守护进程，让容器拿到宿主真实上网网卡"
  echo "   ./start.sh --local        强制本地 Python 起 Web（自动建 .venv 并装依赖）"
  echo "   ./start.sh --full         完整采集：编译 C++ + 起 D-Bus + 起 Web（需 Linux/WSL2）"
  echo "   ./start.sh --check        只做环境检查，不启动"
  exit 0
}

# ---- 模式解析 ----
MODE="auto"
DO_CHECK=0
for a in "$@"; do
  case "$a" in
    --docker) MODE="docker_web" ;;
    --docker-web) MODE="docker_web" ;;
    --docker-full) MODE="docker_full" ;;
    --local)  MODE="local" ;;
    --full)   MODE="full" ;;
    --check)  DO_CHECK=1 ;;
    --help|-h) usage ;;
    *) die "不认识的参数：$a （用 --help 看用法）" ;;
  esac
done

# ---- 环境检查 ----
echo "=== NetPulse 环境检查 ==="
PY_OK=0
if has python3; then
  PY_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  info "Python $PY_VER 已找到"
  PY_OK=1
else
  warn "没找到 python3"
fi

DOCKER_OK=0
if has docker; then
  if docker compose version >/dev/null 2>&1; then
    info "Docker + Compose v2 已找到"
    DOCKER_OK=1
  elif has docker-compose; then
    info "Docker 已找到（旧版 docker-compose）"
    DOCKER_OK=1
  else
    warn "有 docker 但没有 compose 插件（装一下 docker compose plugin）"
  fi
else
  warn "没找到 docker（想零依赖起 Web 就装一个）"
fi

OS="$(uname -s 2>/dev/null || echo unknown)"
if [ "$OS" = "Linux" ]; then
  info "运行在 Linux/WSL2 —— 支持完整内核采集"
else
  warn "运行在 $OS —— 只能跑体验 / Web 模式，eBPF 真实采集需要 Linux/WSL2"
fi

if [ "$DO_CHECK" -eq 1 ]; then
  echo
  if [ "$DOCKER_OK" -eq 1 ]; then
    info "建议：./start.sh --docker（体验 Web，零依赖）或 ./start.sh --docker-full（真实采集宿主网卡）"
  elif [ "$PY_OK" -eq 1 ]; then
    info "建议：./start.sh --local（会自动建 .venv 装依赖）"
  else
    die "既没 docker 也没 python3，先装一个再说"
  fi
  exit 0
fi

# ---- 本地 Python Web 的准备（建 venv + 装依赖） ----
ensure_venv() {
  if [ ! -d "$VENV" ]; then
    info "建虚拟环境 $VENV …"
    python3 -m venv "$VENV" \
      || die "建 venv 失败（可能缺 python3-venv：sudo apt install python3-venv）"
  fi
}

install_web_deps() {
  info "安装 Web 依赖（来自 web/requirements.txt）…"
  "$VENV/bin/python" -m pip install -r "$ROOT_DIR/web/requirements.txt" \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    || warn "依赖安装失败（可能没网）。若 Python 里已能 import fastapi，可以忽略。"
}

prepare_web() {
  ensure_venv
  if ! "$VENV/bin/python" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    install_web_deps
  fi
}

start_web_local() {
  prepare_web
  info "启动 Web（本地 Python，演示模式无需 Key）…"
  info "浏览器打开 http://127.0.0.1:$WEB_PORT  （可加 ?demo 直接看演示）"
  cd "$ROOT_DIR/web"
  PYTHONPATH="$ROOT_DIR" \
  "$VENV/bin/python" -m uvicorn app:app --host 127.0.0.1 --port "$WEB_PORT"
}

start_web_docker() {
  info "用 Docker 起 Web（宿主端零依赖）…"
  cd "$ROOT_DIR/web"
  if docker compose version >/dev/null 2>&1; then
    docker compose up --build || die "Docker 启动失败，看上面报错"
  elif has docker-compose; then
    docker-compose up --build || die "Docker 启动失败，看上面报错"
  else
    die "没有可用的 compose（装 docker compose plugin，或用旧版 docker-compose）"
  fi
  info "浏览器打开 http://127.0.0.1:$WEB_PORT  （可加 ?demo 直接看演示）"
}

# ---- 全栈 Docker（含宿主桥接守护进程）----
start_full_docker() {
  info "全栈 Docker：编译 + eBPF + D-Bus + Web（宿主端零编译依赖）…"

  # 桥接守护进程：常驻宿主，独立于容器和本脚本生命周期。
  # 这里【始终重启】它——先杀掉可能用旧代码跑着的实例，再用最新代码起，
  # 保证你改完 daemon 后只要跑一次本脚本就能用上新逻辑。nohup + disown 使其
  # 脱离本 shell 存活（关终端 / 停 docker 都不会断桥）。
  DAEMON_PORT="${NETPULSE_BRIDGE_PORT:-18900}"
  info "重启宿主桥接守护进程（agent/host_bridge_daemon.py，端口 $DAEMON_PORT，常驻后台）…"
  pkill -f host_bridge_daemon.py 2>/dev/null || true
  sleep 1
  NETPULSE_FORCE_BRIDGE=1 \
  PYTHONPATH="$ROOT_DIR" \
  nohup python3 "$ROOT_DIR/agent/host_bridge_daemon.py" "$DAEMON_PORT" \
    > /tmp/netpulse-bridge.log 2>&1 &
  disown 2>/dev/null || true
  for _ in $(seq 1 20); do
    if curl -s -o /dev/null "http://127.0.0.1:$DAEMON_PORT/healthz" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if curl -s -o /dev/null "http://127.0.0.1:$DAEMON_PORT/healthz" 2>/dev/null; then
    info "桥接守护进程就绪（GET /host-net 可用）"
  else
    warn "桥接守护进程可能没起来（看 /tmp/netpulse-bridge.log）；容器将拿不到宿主网卡，可手动："
    warn "  NETPULSE_FORCE_BRIDGE=1 nohup python3 '$ROOT_DIR/agent/host_bridge_daemon.py' $DAEMON_PORT > /tmp/netpulse-bridge.log 2>&1 &"
  fi

  # 把 WSL2 实例 IP 也告诉容器，作为 host.docker.internal 之外的更稳直连地址
  # （Docker Desktop 容器直连 WSL2 实例 IP，通常比 host.docker.internal 可靠）。
  WSL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [ -n "$WSL_IP" ]; then
    export NETPULSE_BRIDGE_HOST="$WSL_IP"
    info "容器桥接直连地址：http://$WSL_IP:$DAEMON_PORT/host-net"
  fi

  cd "$ROOT_DIR"
  if docker compose version >/dev/null 2>&1; then
    docker compose up --build || die "Docker 启动失败，看上面报错"
  elif has docker-compose; then
    docker-compose up --build || die "Docker 启动失败，看上面报错"
  else
    die "没有可用的 compose（装 docker compose plugin，或用旧版 docker-compose）"
  fi
  info "浏览器打开 http://127.0.0.1:8000"
  info "停 Docker：在本终端按 Ctrl-C（只停容器；桥接守护进程常驻）"
  info "停守护进程：pkill -f host_bridge_daemon.py"
  # 注意：不再 trap 杀守护进程——让它常驻，容器随时能取宿主网卡。
}

# ---- 完整采集模式 ----
start_full() {
  [ "$OS" = "Linux" ] || die "完整采集需要 Linux/WSL2（当前 $OS 不行）"
  has make      || die "没找到 make（sudo apt install build-essential）"
  has dbus-send || die "没找到 dbus-send（sudo apt install dbus）"

  info "编译 C++ 引擎（make）…"
  ( cd "$ROOT_DIR" && make ) || die "编译失败，看上面的报错"

  prepare_web

  info "起 D-Bus + 服务端（首次 setcap 会要一次 sudo）…"
  # run-dbus-server.sh 会先把地址写进 .dbus-address，末尾前台 exec 服务端。
  # 这里后台拉起它，再去读地址喂给 Web。
  "$ROOT_DIR/run-dbus-server.sh" &
  SRV_PID=$!
  ADDR_FILE="$ROOT_DIR/.dbus-address"
  for _ in $(seq 1 30); do
    [ -f "$ADDR_FILE" ] && break
    sleep 1
  done
  ADDR="$(cat "$ADDR_FILE" 2>/dev/null || true)"
  [ -n "$ADDR" ] || die "没拿到 D-Bus 地址（run-dbus-server.sh 可能失败，看上面输出）"

  # 建检索索引（缺失才建；没 Key 会自动降级成离线伪向量）
  if [ ! -f "$ROOT_DIR/rag/index_store/chunks.json" ]; then
    info "建检索索引（rag/build_index.py --sample）…"
    ( cd "$ROOT_DIR/rag" && PYTHONPATH="$ROOT_DIR" python3 build_index.py --sample ) \
      || warn "建索引失败（不影响启动，Web 演示 / 知识库检索会降级）"
  fi

  info "启动 Web（连真实采集）…"
  cd "$ROOT_DIR/web"
  PYTHONPATH="$ROOT_DIR" DBUS_SESSION_BUS_ADDRESS="$ADDR" \
  "$VENV/bin/python" -m uvicorn app:app --host 127.0.0.1 --port "$WEB_PORT" &
  WEB_PID=$!

  info "全部起来了。浏览器打开 http://127.0.0.1:$WEB_PORT"
  info "停服务：在本终端按 Ctrl-C（会一起结束服务端和 Web）"
  trap 'kill $SRV_PID $WEB_PID 2>/dev/null' EXIT INT TERM
  wait
}

# ---- 分发 ----
case "$MODE" in
  docker_web) start_web_docker ;;
  docker_full) start_full_docker ;;
  local)  start_web_local ;;
  full)   start_full ;;
  auto)
    if [ "$DOCKER_OK" -eq 1 ]; then start_web_docker
    elif [ "$PY_OK" -eq 1 ]; then start_web_local
    else die "既没 docker 也没 python3，先装一个再说"; fi
    ;;
esac
