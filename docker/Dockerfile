# NetPulse 全栈镜像
#
# 一条 `docker compose up --build` 同时完成：
#   1. 在镜像内编译 C++ 引擎 + eBPF 对象（宿主端不用装 clang/cmake/C++）
#   2. 容器内起 D-Bus 会话总线 + C++ 服务端 + Web
#
# 单阶段写法是为了"一定能编、一定能跑"——后续可按需拆成 builder/runtime 多阶段瘦身。
# eBPF 需要特权容器（privileged）；这是本地诊断镜像，端口只绑 127.0.0.1。

# 钉到 bookworm（Debian 稳定版），拿一个已知良好、稳定的 C++/eBPF 工具链（clang、libbpf 等）。
# 注意：`python:3.12-slim` 这个标签【固定装 Python 3.12】（装在 /usr/local），
# 而且官方 python 镜像自带 3.12 的开发头文件（/usr/local/include/python3.12/Python.h），
# 所以【不要】在 apt 里装任何 `python3*-dev`：
#   - bookworm 的 apt 默认 python 是 3.11，`python3.12-dev` 包根本不存在（构建会 exit 100）；
#   - trixie 的 apt 默认 python 是 3.13，`python3.12-dev` 同样不存在，且即使有也只是 3.13 的头。
# 真正给 dbus-python 编译用的 3.12 头，来自官方镜像自带的 /usr/local/include/python3.12。
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# 构建 + 运行时依赖（libgoogle-glog-dev 等 -dev 包会顺带装好运行时 .so）
#   build-essential  clang   编译 C++ / eBPF 对象
#   libdbus-1-dev           D-Bus 开发头与库
#   libgoogle-glog-dev      glog（服务端日志）
#   libbpf-dev libelf-dev    eBPF / BPF 对象加载
#   libcap-dev              setcap（容器内若换非 root 用户会用到）
#   dbus                    运行时 dbus-daemon（会话总线）
#   libglib2.0-dev          dbus-python 现场编译时的依赖（优先用 wheel，无 wheel 再编译）
#   libgomp1                faiss-cpu 运行时的 OpenMP 依赖
# 注意：dbus 的 Python 绑定【不能】用 apt 的 python3-dbus —— 那个只装到 Debian 系统
# 自带的 python3.11（/usr/bin/python3），而本镜像的 python3 是 /usr/local 的 3.12。
# 必须 pip install dbus-python（见下方 RUN），才能给 3.12 装上 import dbus。
# dbus-python 是 C 扩展，编译时需要 **Python 开发头文件**。但【不要】靠 apt 装
# `python3-dev` / `python3.12-dev`：bookworm/trixie 的 apt 里根本没有 python3.12-dev
# （只有 3.11/3.13 的头，对 3.12 无效）。官方 python:3.12 镜像已自带 3.12 头文件
# （/usr/local/include/python3.12/Python.h），pip 编译时通过 `python3-config --includes`
# 自动找到，无需任何额外 apt 包。下方 apt 列表里【刻意不写】任何 python3*-dev。
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential clang \
    pkg-config \
    libdbus-1-dev libgoogle-glog-dev libbpf-dev libelf-dev zlib1g-dev libcap-dev \
    dbus libglib2.0-dev libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Python 依赖（agent + rag + web 全在这一份里）
RUN pip install --no-cache-dir -r web/requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/

# dbus 的 Python 绑定：给镜像里的 python3.12 装（pip 走 manylinux 预编译 wheel，
# 若当前平台无 wheel 则现场编译，已具备 libdbus-1-dev + libglib2.0-dev + pkg-config）。
RUN pip install --no-cache-dir dbus-python \
    -i https://mirrors.aliyun.com/pypi/simple/

# 编译 C++ 引擎 + eBPF 对象（vmlinux.h 已内置，无需系统内核头）
RUN make

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
