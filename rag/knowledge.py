"""
知识库接入 —— 复用项目里已有的 network_knowledge_base.py，不重写一份。

它和日志 chunk 是两个不同性质的语料：

    日志 chunk     实例性、有时效   "10:00:30 eth0 的 RTT 涨到 210ms"
    知识 chunk     通用性、无时效   "RTT 超过 100ms 属于严重延迟"

一个 RAG 系统两类都要有，缺一不可：
只有日志，模型能说"发生了什么"但说不出"为什么、怎么办"；
只有知识，模型能讲道理但对不上这台机器此刻的真实状态。

检索时它们会被同一个 query 一起召回，在 RRF 阶段自然竞争排名 ——
这也是混合检索的一个隐性好处：**打分函数天然支持异构语料**，
换成按分数加权的话，日志和知识库的分数尺度不一样，又得单独调。
"""

from __future__ import annotations

import os
import sys
from typing import Any

# 知识库文件在隔壁的 "AI-assisted analysis" 目录里（带空格，不能作为包导入），
# 所以用 sys.path 挂载的方式引进来，避免复制一份造成两边不同步。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 候选目录名，按优先级。
#
# 为什么是一串而不是一个：这个目录被改过名。原来叫 "AI-assisted analysis"，
# 后来归档成了 "AI-assisted analysis_old"，但**空目录 "AI-assisted analysis"
# 还在**。写死单个路径的后果很隐蔽 —— isdir() 对空目录返回 True，
# 于是既不报"找不到"，也 import 不到东西，最后静默退化成"只用日志检索"。
#
# 症状是：检索还在跑、索引还能建，但**重建索引后知识库那部分悄悄没了**，
# 模型开始答不出"这个指标正常范围是多少"。
#
# 所以判据不能是"目录在不在"，必须是"目录里有没有那个文件"。
_KB_DIR_CANDIDATES = [
    "AI-assisted analysis",
    "AI-assisted analysis_old",
    "AI-assisted analysis.bak",
]


def find_knowledge_dir() -> str | None:
    """找到真正含有 network_knowledge_base.py 的那个目录。找不到返回 None。"""
    # 1) 先按候选名单找
    for name in _KB_DIR_CANDIDATES:
        candidate = os.path.join(_PROJECT_ROOT, name)
        if os.path.isfile(os.path.join(candidate, "network_knowledge_base.py")):
            return candidate

    # 2) 再兜底扫一遍：任何以 "AI-assisted" 开头的目录都可以，
    #    这样以后再改名也不用回来改代码。
    try:
        for entry in sorted(os.listdir(_PROJECT_ROOT)):
            if not entry.startswith("AI-assisted"):
                continue
            candidate = os.path.join(_PROJECT_ROOT, entry)
            if os.path.isfile(os.path.join(candidate, "network_knowledge_base.py")):
                return candidate
    except OSError:
        pass

    return None


def load_knowledge() -> dict[str, Any]:
    """加载网络知识库。找不到就返回空字典，让上层降级成"只用日志检索"。

    注意：这里的提示信息**故意不用 emoji**。
    这是个库函数，调用方不一定做过控制台编码初始化（见 console.py）——
    在 Windows 上从没初始化过的进程里 print 一个 emoji 会直接抛
    UnicodeEncodeError，把一次"温和降级"变成一次崩溃。
    """
    kb_dir = find_knowledge_dir()
    if kb_dir is None:
        print(
            "[warn] 没找到知识库（network_knowledge_base.py），将只索引日志。\n"
            f"       找过这些位置：{_KB_DIR_CANDIDATES[:2]} 以及任何 AI-assisted* 目录\n"
            f"       项目根目录：{_PROJECT_ROOT}"
        )
        return {}

    if kb_dir not in sys.path:
        sys.path.insert(0, kb_dir)

    try:
        from network_knowledge_base import get_network_knowledge  # type: ignore
    except ImportError as exc:
        print(f"[warn] 导入知识库失败（{exc}），将只索引日志")
        return {}

    return get_network_knowledge()


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()

    found = find_knowledge_dir()
    print(f"知识库目录：{found}")
    kb = load_knowledge()
    print(f"知识库大类：{len(kb)} 个")
    for category, content in kb.items():
        desc = content.get("description", "") if isinstance(content, dict) else ""
        print(f"  {category:24s} {desc}")
