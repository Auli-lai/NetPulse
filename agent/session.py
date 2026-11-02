"""
会话记忆 —— 多轮问答里"上一句问了什么"。

===========================================================================
它和长期记忆的区别：一个管指代，一个管经验
===========================================================================

    长期记忆   跨会话。回答"我以前见过这个吗"
               召回的是**结论**（什么现象对应什么根因）

    会话记忆   一次会话内。回答"他刚刚说的是哪个问题"
               记住的是**问题本身**，不是结论

举个最能说明区别的例子：

    用户：eth0 最近有点卡
    助手：（诊断一轮，给出结论）
    用户：那 wlan0 呢？          ← 这一句里的"那"指代上一轮

第二句如果不知道上一句，根本没法理解 —— 这就是会话记忆要解决的。
而如果用户第二天回来说"eth0 又卡了"，那是长期记忆要解决的。

**两者的失效方式也不同：**
  · 会话记忆太长 → 上下文爆掉、注意力被稀释 → 所以要**只留最近几轮**
  · 长期记忆太杂 → 召回不准、把无关经验当成参考 → 所以要**去重 + 淘汰**

===========================================================================
为什么只存"问题和结论"，不存完整的工具轨迹
===========================================================================

一次诊断的完整轨迹可能有 5 轮、十几个工具调用、几万字符。
如果每次多轮问答都把上一次的完整轨迹带进上下文：

  1. **贵** —— token 成本线性增长
  2. **反而更差** —— 模型会被上一轮的工具原始输出吸引注意力，
     而真正该参考的是"上一轮得出了什么结论"

所以这里的取舍是：**会话层只留结论，过程留在它自己那一次诊断里。**

这和记忆的分层是同一个道理：**过程、上下文、结论该分开管**（见 memory.py）。
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Sequence

# 默认保留几轮。3 轮足够消解指代，又不至于把上下文撑大。
DEFAULT_MAX_TURNS = int(os.environ.get("NETPULSE_SESSION_TURNS", "3"))

# 注入时每条结论截断到多少字
_ANSWER_CLIP = 220


@dataclass
class Turn:
    question: str
    answer: str
    interfaces: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Session:
    """一次会话（一个交互式进程）里的多轮上下文。

    用法::

        sess = Session()
        result = agent.run("eth0 最近有点卡", session=sess)
        result = agent.run("那 wlan0 呢？", session=sess)   # 知道"那"指什么
    """

    def __init__(self, max_turns: int = DEFAULT_MAX_TURNS) -> None:
        self.max_turns = max_turns
        self._turns: deque[Turn] = deque(maxlen=max_turns)

    # ---------------- 写 ----------------

    def add(self, question: str, answer: str, interfaces: Sequence[str] | None = None) -> None:
        if not (question or "").strip():
            return
        self._turns.append(
            Turn(
                question=question.strip(),
                answer=(answer or "").strip(),
                interfaces=list(interfaces) if interfaces else None,
            )
        )

    def clear(self) -> None:
        self._turns.clear()

    # ---------------- 读 ----------------

    def render(self, max_chars_per_turn: int = _ANSWER_CLIP) -> str:
        """渲染成注入提示词的文本。没有历史就返回空串。

        ⚠️ 每一条结论都**必须截断**。不截断的话，多轮问答会把
        上下文撑得很大，而且模型容易被上一轮的细节带跑 ——
        真正需要它记住的只是"上一轮聊的是哪个问题、结论是什么"。
        """
        if not self._turns:
            return ""

        lines = ["【本次会话已经聊过（用来理解『它』『刚才那个』这类指代）】"]
        for i, turn in enumerate(self._turns, 1):
            answer = turn.answer.replace("\n", " ").strip()
            if len(answer) > max_chars_per_turn:
                answer = answer[:max_chars_per_turn] + "…"
            iface = f"（涉及网卡 {', '.join(turn.interfaces)}）" if turn.interfaces else ""
            lines.append(f"{i}. 问：{turn.question}{iface}")
            lines.append(f"   结论：{answer}")
        return "\n".join(lines)

    def turns(self) -> list[Turn]:
        return list(self._turns)

    def __len__(self) -> int:
        return len(self._turns)

    def last(self) -> Turn | None:
        return self._turns[-1] if self._turns else None

    # ---------------- 落盘（可选） ----------------

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"max_turns": self.max_turns, "turns": [t.to_dict() for t in self._turns]},
                f,
                ensure_ascii=False,
                indent=2,
            )

    @classmethod
    def load(cls, path: str) -> "Session":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            return cls()
        sess = cls(max_turns=int(payload.get("max_turns", DEFAULT_MAX_TURNS)))
        for item in payload.get("turns", []):
            sess.add(item.get("question", ""), item.get("answer", ""), item.get("interfaces"))
        return sess


if __name__ == "__main__":
    import sys

    # session.py 本身不依赖 rag/，只有这个自检要用它的 console 工具，
    # 所以路径在这里挂而不是放在模块顶层。
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rag"))
    from console import enable_utf8_output

    enable_utf8_output()

    sess = Session(max_turns=3)
    print("空会话渲染：", repr(sess.render()))
    print()

    sess.add("eth0 最近有点卡", "## 根因判断\neth0 的 RTT 升到 210ms，判断为链路质量问题。" * 5,
             interfaces=["eth0"])
    sess.add("那 wlan0 呢？", "wlan0 的 RSSI 是 -55dBm，信号良好，没有问题。",
             interfaces=["wlan0"])

    print(f"会话里有 {len(sess)} 轮\n")
    print(sess.render(max_chars_per_turn=80))
    print()

    # 超过 max_turns 时最旧的会被挤掉 —— 这是刻意的
    sess.add("第三个问题", "结论三")
    sess.add("第四个问题", "结论四")
    print(f"又加了两轮（max_turns=3），现在剩 {len(sess)} 轮，最旧的是：")
    print("  ", sess.turns()[0].question)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.json")
        sess.save(path)
        reloaded = Session.load(path)
        print(f"\n落盘再读回：{len(reloaded)} 轮，最后一条 = {reloaded.last().question}")
