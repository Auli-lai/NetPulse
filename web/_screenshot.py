"""
前端视觉检查 —— 自动开浏览器跑一次诊断并截图。

    python _screenshot.py

===========================================================================
为什么需要这个
===========================================================================

语法检查（node --check）只能证明 JS **能解析**，证明不了：

  · 有没有运行时报错（元素 id 写错、事件名拼错 —— 一跑就崩）
  · 布局有没有塌（标签重叠、内容溢出、时间线的竖线错位）
  · 深色模式是不是真的可用（不是"把浅色反一下"就行）

这三类问题**必须真的渲染出来看**才能发现。所以这个脚本用真实浏览器
打开页面、跑一次完整诊断、截两张图（浅色 + 深色），
并把浏览器控制台里的报错原样打出来。

===========================================================================
用的是系统自带的浏览器，不是 Playwright 下载的
===========================================================================

`playwright install` 要从 Google 的 CDN 下 Chromium，国内基本下不动。
但 Playwright 可以直接驱动**已经装好的** Edge / Chrome：

    p.chromium.launch(channel="msedge")

Windows 自带 Edge，所以这个脚本在没网的环境也能跑。
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent

PORT = 8199
BASE = f"http://127.0.0.1:{PORT}"


def _wait_port(port: int, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def _pick_channel() -> str | None:
    """挑一个本机装了的浏览器。找不到就返回 None（让 Playwright 用它自带的）。"""
    candidates = [
        (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "msedge"),
        (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "msedge"),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
        (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "chrome"),
    ]
    for path, channel in candidates:
        if os.path.exists(path):
            return channel
    return None


async def shoot() -> int:
    from playwright.async_api import async_playwright

    channel = _pick_channel()
    print(f"使用浏览器：{channel or 'Playwright 自带 chromium'}")

    console_errors: list[str] = []
    page_errors: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel=channel, headless=True, args=["--no-sandbox"]
        )
        page = await browser.new_page(viewport={"width": 1000, "height": 1400})

        # 浏览器控制台里的报错 = JS 运行时错误。这是语法检查抓不到的那一类。
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # 带上 ?demo，否则会去调真实模型 —— 在没有 API Key / SDK 的机器上直接报错。
        # 这个坑正是第一次跑这个脚本时踩到的：以为前端坏了，其实是没开演示模式。
        print(f"打开 {BASE}/?demo")
        await page.goto(f"{BASE}/?demo", wait_until="networkidle")

        # 触发一次演示诊断（点第一个示例 chip）
        await page.click(".chip")
        print("已点击示例问题，等待诊断完成…")

        # 等到状态文字变成"完成"（或超时）
        try:
            await page.wait_for_function(
                "() => document.getElementById('statusText').textContent.includes('完成')",
                timeout=30000,
            )
        except Exception:
            print("  [warn] 等待超时，仍然截图（看看卡在什么状态）")

        await page.wait_for_timeout(800)

        # 展开所有工具返回，截图更有信息量
        await page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        await page.wait_for_timeout(400)

        # 存到 docs/ 而不是就地 —— README 要引用它们
        out_dir = _PROJECT / "docs"
        out_dir.mkdir(exist_ok=True)
        light = out_dir / "screenshot-light.png"
        await page.screenshot(path=str(light), full_page=True)
        print(f"  已保存 {light.name}")

        # 深色模式
        await page.click("#theme")
        await page.click("#theme")
        await page.wait_for_timeout(500)
        dark = out_dir / "screenshot-dark.png"
        await page.screenshot(path=str(dark), full_page=True)
        print(f"  已保存 {dark.name}")

        # 顺手把页面上实际渲染出来的关键内容报一下，方便断言
        summary = await page.evaluate("""() => {
            const kpi = (label) => {
                for (const el of document.querySelectorAll('.kpi')) {
                    if (el.querySelector('.label').textContent.trim() === label)
                        return el.querySelector('.value').textContent.trim();
                }
                return null;
            };
            return {
                status: document.getElementById('statusText').textContent,
                nodes: document.querySelectorAll('.node').length,
                toolRows: document.querySelectorAll('.node.good, .node.error, .node.warn').length,
                kpiCount: document.getElementById('kpis').hidden
                          ? 0 : document.querySelectorAll('.kpi').length,
                kpiRounds: kpi('推理轮次'),
                kpiTools: kpi('工具调用'),
                answerLen: (document.querySelector('.card.answer .md') || {}).textContent?.length || 0,
                answerNodes: document.querySelectorAll('.card.answer').length,
                horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 2,
                // 深色模式是否真的切换了（不是只改了按钮文字）
                theme: document.documentElement.getAttribute('data-theme'),
            };
        }""")

        print("\n渲染结果：")
        for k, v in summary.items():
            print(f"  {k:20s} {v}")

        await browser.close()

    # ---- 断言。这些是"页面看起来对但其实错了"的那类问题 ----
    failures: list[str] = []
    if page_errors:
        failures.append(f"页面抛了 {len(page_errors)} 个 JS 异常：" + "; ".join(page_errors[:3]))
    if summary["answerLen"] < 20:
        failures.append("结论没渲染出来（答案区是空的）")
    if summary["answerNodes"] == 0:
        failures.append("没有结论卡片")
    if summary["nodes"] < 5:
        failures.append(f"时间线节点太少（{summary['nodes']}），事件可能没渲染全")
    if summary["toolRows"] < 3:
        failures.append(f"工具行只有 {summary['toolRows']} 条，演示脚本里有 5 次调用")
    if str(summary["kpiRounds"]) in ("0", "None", "null"):
        failures.append(f"KPI『推理轮次』是 {summary['kpiRounds']}，但时间线上明明有思考节点"
                        " —— 计数器没接上")
    if not summary["kpiTools"] or summary["kpiTools"] == "0":
        failures.append(f"KPI『工具调用』是 {summary['kpiTools']}")
    if summary["horizontalOverflow"]:
        failures.append("页面出现横向溢出")
    if summary["theme"] != "dark":
        failures.append(f"主题切换没生效（data-theme={summary['theme']}）")

    print()
    if console_errors:
        print(f"[warn] 控制台有 {len(console_errors)} 条 error 级日志：")
        for e in console_errors[:5]:
            print("   ", e[:180])
    else:
        print("[OK] 无控制台报错")

    if failures:
        print(f"\n[FAIL] {len(failures)} 项：")
        for f in failures:
            print(f"   · {f}")
        return 1
    print("[OK] 全部检查通过")
    return 0


def main() -> int:
    sys.path.insert(0, str(_PROJECT / "rag"))
    from console import enable_utf8_output

    enable_utf8_output()

    env = {**os.environ, "RAG_OFFLINE": "1"}
    print(f"启动服务 :{PORT} …")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(_HERE),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_port(PORT):
            print("服务没能启动")
            return 2
        print("服务已就绪\n")
        return asyncio.run(shoot())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
