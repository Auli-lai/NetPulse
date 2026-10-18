"""
控制台输出编码 —— 一个在 Windows 上不修就一定会崩的坑。

===========================================================================
现象：终端里跑没事，一加管道就崩
===========================================================================

    $ python bm25.py                            # 正常
    $ python bm25.py | tail -5                  # UnicodeEncodeError 崩

    UnicodeEncodeError: 'gbk' codec can't encode character '✅'

同一个程序，加不加 `| tail` 结果不同。原因在 stdout 是什么：

  · 输出到终端 —— Python 用 _WindowsConsoleIO，直接往控制台写 UTF-16，
    **编码器根本不参与**，emoji / 中文 / 制表符全都正常。
  · 输出到管道或文件 —— stdout 变成普通文本流，Python 改用**本地码页**
    （简体中文 Windows 上是 GBK / cp936）编码，而且 errors='strict'。
    GBK 里没有 ✅ ⚠️ ❌，编不出来就抛异常。

这就是它难查的地方：**你手工怎么测都是好的**，一旦 `| tee`、`> out.txt`、
或者被别的程序 subprocess 调起来、或者进了 CI，立刻就炸。而且崩在 print
那一行，看上去和业务逻辑毫无关系。

顺带记一句：中文本身 GBK 是有的，会出问题的只有 emoji、制表符、
数学符号这一类。但"显示成问号"和"进程直接挂"是两码事 —— 后者会让
管道下游拿到半截输出，问题被掩盖得更深。

===========================================================================
修法：分两种情况，不能只做一半
===========================================================================

  1. 输出到管道/文件
     没有终端在解释这些字节，直接换 UTF-8 就是对的。

  2. 输出到真终端
     只把 Python 这边改成 UTF-8 **不够** —— 终端还在按 GBK 解释我们写
     出去的字节，中文会整片变乱码。必须先用 SetConsoleOutputCP(65001)
     把**终端自己**的码页也切到 UTF-8。

第 2 步万一失败（非 Windows、权限受限、无控制台），就不能硬上 UTF-8 了，
退回原编码 + errors='replace'：emoji 变 "?"，但至少不乱码、不崩溃。

===========================================================================
用法
===========================================================================

每个可执行脚本的入口调用一次即可：

    if __name__ == "__main__":
        from console import enable_utf8_output

        enable_utf8_output()
        main()

放在 `__main__` 里而不是模块顶层，是因为在 import 时去改全局的
stdout 是很失礼的行为 —— 别人 import 你的模块不应该顺手改掉他的输出流。
"""

from __future__ import annotations

import sys

__all__ = ["enable_utf8_output"]


def enable_utf8_output() -> None:
    """让 print 出来的 emoji / 中文在管道、重定向、终端下都不出错。

    幂等，可以放心重复调用。
    """
    streams = [s for s in (sys.stdout, sys.stderr) if hasattr(s, "reconfigure")]
    if not streams:
        return

    encoding: str | None = "utf-8"

    # 只有"真终端"才需要动码页；管道和重定向没有终端在解释字节。
    # isatty() 为 False 时反而必须用 UTF-8 —— 下游（编辑器、grep、
    # 另一段程序）默认按 UTF-8 解码，这时候写 GBK 才是错的。
    if sys.platform == "win32" and sys.stdout.isatty():
        if not _set_windows_console_utf8():
            # 码页没切成功。这时若仍写 UTF-8，终端会按 GBK 去解，
            # 中文全部变成乱码 —— 比崩掉更难排查。
            # 所以保留原编码，只把编不了的字符降级成 "?"。
            encoding = None

    for stream in streams:
        try:
            if encoding is None:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            else:
                stream.reconfigure(encoding=encoding, errors="replace")  # type: ignore[union-attr]
        except (OSError, ValueError):
            # 流被替换成了不支持 reconfigure 的对象（比如 pytest 的捕获器）。
            # 这种情况本来也不会崩，忽略即可。
            pass


def _set_windows_console_utf8() -> bool:
    """把 Windows 控制台的输出码页切成 UTF-8。成功返回 True。"""
    try:
        import ctypes

        # 65001 = CP_UTF8。返回值 0 表示失败。
        return bool(ctypes.windll.kernel32.SetConsoleOutputCP(65001))
    except Exception:
        return False


if __name__ == "__main__":
    enable_utf8_output()

    print(f"stdout.encoding = {sys.stdout.encoding}")
    print(f"stdout.isatty() = {sys.stdout.isatty()}")
    print("以下字符能打出来就没问题：")
    print("  ✅ 成功   ⚠️ 警告   ❌ 失败   · 分隔   → 箭头   ① 序号")
    print("  中文：混合检索 RRF 融合 重排 —— 网卡 eth0 丢包率 3.2%")
