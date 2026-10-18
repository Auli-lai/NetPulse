# NetPulse 全栈镜像：C++ 服务端（eBPF）+ 总线 + Python Web，一个容器全包。
#
# 构建（上下文是**项目根目录**，因为要 server/ agent/ rag/ web/ 全拷进去）：
#
#     docker build -f deploy/Dockerfile -t netpulse:latest .
#     或用 compose：cd deploy && docker compose up --build
#
# 运行：
#
#     docker run --rm -p 8000:8000 -e QWEN_API_KEY=sk-... netpulse:latest
#     打开 http://127.0.0.1:8000/?demo 可以不需要 key 先看界面
#
# ⚠️ 诚实的限制（README「上线与分发」一节有完整版）：
#   · eBPF 是 Linux 内核特性。Docker Desktop（Windows/macOS）的 Linux VM
#     内核看不到你物理机的网卡 —— 容器里能跑，但监控的是容器/veth，不是你的 WiFi
#   · 要监控**真实网卡**：原生 Linux + network_mode: host + privileged: true；
#     或者干脆别用容器，在宿主机 python start.py
#   · 两条都不满足也能用：服务端优雅降级，Web/Agent/检索/演示模式全好

# ---------------------------------------------------------------------------
# 阶段 1：编译 C++ 服务端 + eBPF 对象
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        make \
        clang \
        llvm \
        libdbus-1-dev \
        libglog-dev \
        libbpf-dev \
        libelf-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .

# 只编服务端（server/Makefile 自包含，不需要根 Makefile 的 config.mk）。
# clang -target bpf 编 flow_rate.bpf.o；libbpf 在运行阶段才需要，这里只编。
RUN make -C server -j"$(nproc)"

# ---------------------------------------------------------------------------
# 阶段 2：运行环境
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# dbus         总线与服务端探测（dbus-daemon / dbus-send）
# libgomp1     faiss-cpu 的 OpenMP 运行时，缺了它 ImportError 只报 "No module named faiss"
# curl         healthcheck 用
RUN apt-get update && apt-get install -y --no-install-recommends \
        dbus \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /src/server/bin/weaknet-dbus-server /app/server/bin/
COPY --from=builder /src/server/build/flow_rate.bpf.o     /app/server/build/
# 二进制要可执行（COPY 默认保留权限，这里兜一手）
RUN chmod 0755 /app/server/bin/weaknet-dbus-server

COPY agent/   /app/agent/
COPY rag/     /app/rag/
COPY web/     /app/web/
COPY deploy/entrypoint.sh /app/entrypoint.sh
COPY .env.example /app/.env.example
RUN chmod +x /app/entrypoint.sh

# 先装依赖再拷代码的层已经没意义了（上面是全量 COPY），但 --no-cache-dir 仍要
RUN pip install --no-cache-dir -r /app/web/requirements.txt

# 中文日志不被 GBK 卡住；-u 让 docker logs 能看到实时进度（调 SSE 时很重要）
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
