#!/usr/bin/env python3
"""
NetPulse 一键启动 —— 一条命令起完全套服务，不需要记任何环境变量。

    python start.py              完整模式：服务端(C++/eBPF) + Web，自动准备
                                 D-Bus 总线和文件能力，自动配好总线地址
    python start.py --lite       轻量模式：不碰 C++/D-Bus/eBPF，psutil 采集
                                 + Web。任何平台（Windows/macOS/Linux）都能跑
    python start.py --web-only   只要 Web（实时工具会不可用，演示模式照常）
    python start.py --port 8080  换端口
    python start.py --no-browser 不自动打开浏览器

===========================================================================
它帮你省掉了什么（这就是本脚本存在的理由）
===========================================================================

完整模式原本要四个终端、记一堆前提：

    终端 1  起一条 session 总线（WSL 默认没有！）
    终端 2  setcap 给二进制（/mnt/e 上还会失败，得拷贝到 ext4 再设）
    终端 3  cd server/bin 起 weaknet-dbus-server
    终端 4  export DBUS_SESSION_BUS_ADDRESS=... 再起 uvicorn

本脚本把这三步全自动做完，而且**总线地址由它注入 Web 进程的环境**——
所以你永远不用再手动 export DBUS_SESSION_BUS_ADDRESS。

和 run-dbus-server.sh 的关系：那个脚本只起服务端（第 3 步），本脚本是
"总线 + 能力 + 服务端 + Web" 的完整编排，逻辑与之一一对应（包括 drvfs
上自动 staged 到 ~/.netpulse-server）。两者可以混用，也可以只用其一。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from typing import Any

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
SERVER_BIN = os.path.join(ROOT, "server", "bin", "weaknet-dbus-server")
BPF_OBJ = os.path.join(ROOT, "server", "build", "flow_rate.bpf.o")
ADDR_FILE = os.path.join(ROOT, ".dbus-address")
STAGE_DIR = os.path.join(os.path.expanduser("~"), ".netpulse-server")
BUS_NAME = "com.example.WeakNet"
DBUS_DEST = "org.freedesktop.DBus"

# 与 run-dbus-server.sh 保持同一组能力
CAPS = "cap_bpf,cap_perfmon,cap_net_admin,cap_net_raw,cap_sys_admin+eip"


def info(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(f"  \033[32m✔\033[0m {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  \033[33m⚠\033[0m {msg}", flush=True)


def die(msg: str, hint: str = "") -> None:
    print(f"\n  \033[31m✘ {msg}\033[0m", file=sys.stderr)
    if hint:
        print(f"    {hint}", file=sys.stderr)
    sys.exit(1)


def run_quiet(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# ---------------------------------------------------------------------------
# D-Bus 总线：找到一条能用的，找不到就起一条
# ---------------------------------------------------------------------------


def bus_works(addr: str) -> bool:
    """真连一下总线，拿到回复才算可用。"""
    if not addr:
        return False
    if not have("dbus-send"):
        # 没有 dbus-send 时只能看 socket 文件在不在（libdbus 会做真正的测试）
        if addr.startswith("unix:path="):
            return os.path.exists(addr[len("unix:path=") :].split(",")[0])
        return False
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=addr)
    proc = run_quiet(
        [
            "dbus-send", "--print-reply",
            "--dest=" + DBUS_DEST, "/org/freedesktop/DBus",
            DBUS_DEST + ".ListNames",
        ],
        env=env,
    )
    return proc.returncode == 0


def ensure_bus() -> str:
    """返回一条可用的总线地址，并写进 os.environ（子进程会继承）。"""
    # 1. 环境里已经有，且真的能用
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if bus_works(addr):
        ok(f"复用环境里的 session 总线：{addr}")
        return addr

    # 2. 上次本脚本起的（.dbus-address 文件）
    if os.path.exists(ADDR_FILE):
        with open(ADDR_FILE, encoding="utf-8") as fh:
            addr = fh.read().strip()
        if bus_works(addr):
            ok(f"复用上次启动的总线（{os.path.basename(ADDR_FILE)}）：{addr}")
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = addr
            return addr

    # 3. 系统默认位置 /run/user/<uid>/bus（有 systemd 用户会话的发行版）
    if sys.platform.startswith("linux"):
        default = f"/run/user/{os.getuid()}/bus"
        if os.path.exists(default) and bus_works(f"unix:path={default}"):
            ok(f"发现系统默认总线：{default}")
            addr = f"unix:path={default}"
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = addr
            return addr

    # 4. 自己起一条（WSL 默认没有 systemd，基本都走到这）
    if not have("dbus-daemon"):
        die(
            "找不到 dbus-daemon，起不了总线。",
            "WSL/Ubuntu 上装一下：sudo apt install dbus",
        )
    proc = run_quiet(["dbus-daemon", "--session", "--fork", "--print-address"])
    addr = (proc.stdout or "").strip().splitlines()[0] if proc.stdout.strip() else ""
    if not addr or not bus_works(addr):
        die("dbus-daemon 起来了但不应答", proc.stderr.strip())
    with open(ADDR_FILE, "w", encoding="utf-8") as fh:
        fh.write(addr)
    ok(f"已启动专用总线并记住地址（{os.path.basename(ADDR_FILE)}）")
    ok(f"地址：{addr}")
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = addr
    return addr


def wait_for_service(addr: str, timeout: float = 20.0) -> bool:
    """等服务端在总线上注册 com.example.WeakNet。"""
    if not have("dbus-send"):
        time.sleep(3)  # 没有探测手段，给个保守的等待
        return True
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=addr)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = run_quiet(
            [
                "dbus-send", "--print-reply",
                "--dest=" + DBUS_DEST, "/org/freedesktop/DBus",
                DBUS_DEST + ".NameHasOwner", "string:" + BUS_NAME,
            ],
            env=env,
        )
        if "boolean true" in (proc.stdout or ""):
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# 文件能力：让服务端能以当前用户身份读 eBPF（逻辑同 run-dbus-server.sh）
# ---------------------------------------------------------------------------


def has_caps(path: str) -> bool:
    if not have("getcap"):
        return False
    proc = run_quiet(["getcap", path])
    return "cap_bpf" in (proc.stdout or "")


def try_setcap(path: str) -> bool:
    if not have("setcap"):
        return False
    if not have("sudo"):
        return False
    proc = run_quiet(["sudo", "setcap", CAPS, path])
    return proc.returncode == 0


def ensure_server_path() -> tuple[str, bool]:
    """返回 (要运行的服务端路径, 是否带齐能力)。

    带不齐也不拦着：服务端照样起，只是 eBPF 流量明细不可用
    （net_traffic.cpp 里 attach 失败是优雅降级的，不会退出）。
    """
    if not os.path.exists(SERVER_BIN):
        die(
            f"没找到服务端二进制：{SERVER_BIN}",
            "先在 WSL/Linux 里编译：make（项目根目录）",
        )
    if has_caps(SERVER_BIN):
        ok("二进制已带所需能力，原地运行")
        return SERVER_BIN, True
    if try_setcap(SERVER_BIN):
        ok("setcap 成功，原地运行")
        return SERVER_BIN, True
    warn("原地 setcap 失败（项目在 /mnt/e 这类 Windows 挂载盘上就会这样）")
    warn(f"自动降级：拷贝到 {STAGE_DIR}（WSL 自己的文件系统）再设能力…")
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    os.makedirs(os.path.join(STAGE_DIR, "bin"), exist_ok=True)
    os.makedirs(os.path.join(STAGE_DIR, "build"), exist_ok=True)
    shutil.copy2(SERVER_BIN, os.path.join(STAGE_DIR, "bin"))
    if os.path.exists(BPF_OBJ):
        shutil.copy2(BPF_OBJ, os.path.join(STAGE_DIR, "build"))
    staged = os.path.join(STAGE_DIR, "bin", "weaknet-dbus-server")
    os.chmod(staged, 0o755)
    if try_setcap(staged):
        ok(f"已在 staged 副本上设好能力：{staged}")
        return staged, True
    warn("setcap 彻底失败 —— 服务端将以**降级模式**运行：")
    warn("接口列表/RTT/丢包仍可用；eBPF 连接级流量(get_flows)不可用。")
    warn("想修：sudo -v 确认能免密 sudo，或把项目挪到 WSL 的 ext4 文件系统再试。")
    return SERVER_BIN, False


# ---------------------------------------------------------------------------
# Web 服务
# ---------------------------------------------------------------------------


def start_web(port: int, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    if not os.path.isdir(WEB_DIR):
        die(f"没找到 web 目录：{WEB_DIR}")
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", str(port)],
        cwd=WEB_DIR,
        env=env,
    )
    return proc


def wait_web(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NetPulse 一键启动（完整 / 轻量 / 只要 Web）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--lite", action="store_true",
                        help="轻量模式：psutil 采集，不需要 C++/D-Bus/eBPF/WSL")
    parser.add_argument("--web-only", action="store_true",
                        help="只起 Web（实时工具不可用，演示模式可用）")
    parser.add_argument("--port", type=int, default=8000, help="Web 端口（默认 8000）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    mode_count = sum([args.lite, args.web_only])
    if mode_count > 1:
        die("--lite 和 --web-only 只能选一个")

    info("")
    info("  NetPulse 启动器")
    info("  " + "─" * 52)

    extra_env: dict[str, str] = {}
    server_proc: subprocess.Popen | None = None

    if args.lite:
        info("  [模式] 轻量（psutil 采集，无需编译/eBPF/D-Bus）")
        check = run_quiet([sys.executable, "-c", "import psutil"])
        if check.returncode != 0:
            die(
                "轻量模式需要 psutil",
                "pip install psutil    （或改用完整模式：python start.py）",
            )
        extra_env["NETPULSE_LITE"] = "1"
        ok("psutil 就绪")

    elif args.web_only:
        info("  [模式] 只要 Web（实时指标类工具会不可用）")

    else:
        info("  [模式] 完整（C++ 服务端 + eBPF + D-Bus + Web）")
        if not sys.platform.startswith("linux"):
            die(
                "完整模式需要 Linux 内核（eBPF 是内核特性，Windows/macOS 上没有）。",
                "三个选择：\n"
                "    · 在 WSL 里跑本脚本（推荐，能同时看到 Windows 宿主 WiFi）\n"
                "    · python start.py --lite   （本机直接跑，psutil 采集）\n"
                "    · Docker：cd deploy && docker compose up --build",
            )
        if os.geteuid() == 0:
            warn("正在以 root 运行。root 连不上普通用户的 session 总线，")
            warn("如果总线异常，请退出后用普通用户重跑。")

        addr = ensure_bus()

        server_path, capped = ensure_server_path()
        # cd 到二进制所在目录：代码里 eBPF 对象路径是相对的
        # "../build/flow_rate.bpf.o"，只有 cwd=.../bin 才解析得到。
        server_cwd = os.path.dirname(server_path)
        info("")
        info(f"  [服务端] 启动 {os.path.basename(server_path)}（cwd={server_cwd}）")
        server_proc = subprocess.Popen(
            [server_path],
            cwd=server_cwd,
            env=dict(os.environ, DBUS_SESSION_BUS_ADDRESS=addr),
        )
        if wait_for_service(addr, timeout=20):
            ok(f"服务端已注册总线名 {BUS_NAME}")
        else:
            if server_proc.poll() is not None:
                die(
                    f"服务端退出了（退出码 {server_proc.returncode}）",
                    "90% 是总线问题：确认上面的总线地址，或手动 ./run-dbus-server.sh 看详细日志",
                )
            warn("20 秒还没等到总线名 —— 服务端可能还在初始化，继续起 Web")

    info("")
    info(f"  [Web] 启动 uvicorn（0.0.0.0:{args.port}，同网段的手机也能访问）")
    web_proc = start_web(args.port, extra_env)
    if not wait_web(args.port, timeout=25):
        if web_proc.poll() is not None:
            die(f"Web 退出了（退出码 {web_proc.returncode}）",
                "先确认依赖装好：pip install -r web/requirements.txt")
        warn("Web 还没响应，继续等它自己起来…")

    url = f"http://127.0.0.1:{args.port}"
    info("")
    ok(f"全部就绪 → {url}")
    if not args.lite and not args.web_only:
        ok("总线地址已由本脚本注入 Web 进程，不需要再手动 export")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    info("")
    info("  停止：Ctrl-C（服务端和 Web 会一起停）")
    info("")

    try:
        while True:
            time.sleep(1)
            if web_proc.poll() is not None:
                warn(f"Web 进程已退出（退出码 {web_proc.returncode}），正在停止服务端…")
                break
            if server_proc is not None and server_proc.poll() is not None:
                warn(f"服务端已退出（退出码 {server_proc.returncode}），正在停止 Web…")
                break
    except KeyboardInterrupt:
        info("\n  收到 Ctrl-C，正在停止…")

    for proc in (web_proc, server_proc):
        if proc is not None and proc.poll() is None:
            proc.terminate()
    for proc in (web_proc, server_proc):
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    ok("已全部停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
