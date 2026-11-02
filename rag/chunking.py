"""
结构感知切分 —— 把日志和知识库切成"检索单元"。

===========================================================================
为什么不能用 RecursiveCharacterTextSplitter(800, 150) 盲切？
===========================================================================

盲切是按**字符数**切的，它不懂数据。对网络日志来说有两个致命后果：

1. 把因果链切断。
   日志里"RTT 从 15ms 升到 210ms"和紧跟其后的"丢包率涨到 3.2%"
   是同一次故障的两个侧面。按 800 字符切，它们的边界正好落在中间，
   于是检索时只能召回半条证据，LLM 拿到的是"RTT 升高了"但不知道
   同时还丢了包 —— 诊断结论直接错。

2. chunk 里混进无关网卡。
   eth0 在丢包、wlan0 一切正常，这两件事被切进同一个 chunk 后，
   向量表示变成两者的平均，谁都不像。

===========================================================================
正确的切法：以"一个诊断单元"为界
===========================================================================

对时序监控数据，自然的语义单元是 **一块网卡 + 一个时间窗**。

    窗口内同一网卡的 RTT / 丢包 / 流量 / RSSI / 质量分 → 一个 chunk

这样每个 chunk 自洽：它描述的就是"eth0 在那 30 秒里发生了什么"。
时间窗长度可调（config.CHUNK_WINDOW_SECONDS）：太短则单条指标凑不成
一次事件，太长则把两次独立抖动缝在一起，一般 30~60 秒合适。

===========================================================================
知识库同理：按"一条知识"切，不按字符切
===========================================================================

network_knowledge_base.py 本身就是结构化的（类别 → 子项 → 内容）。
一个 chunk 取一个子项（比如 rtt_analysis.troubleshooting），
它就是一条完整的"RTT 高怎么排查"。按 800 字符切会把一条建议劈成两半。
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass
class Chunk:
    """一个检索单元。

    text     既用来算向量，也用来建 BM25 索引，还直接喂给 LLM。
             所以它同时要"语义密"和"字面全"（见 _render_window 的注释）。
    metadata 结构化字段，用于过滤、评测标注、以及溯源展示。
    """

    chunk_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 日志行解析
# ---------------------------------------------------------------------------

# glog 前缀： I20260924 10:00:30.123456  1234 file.cc:88]
_GLOG_PREFIX = re.compile(
    r"^[IWEF]\d{8}\s+(\d{2}:\d{2}:\d{2})\.\d+\s+\d+\s+\S+\]\s*"
)
# 方括号时间戳（log_capture.py 的中文格式）： [10:00:30]
_BRACKET_TIME = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")

# 每一条解析规则：(类型, 正则, 字段名列表)
# 字段名列表按顺序对应正则的捕获组（时间戳组除外）。
_LOG_RULES: list[tuple[str, re.Pattern[str], list[str]]] = [
    (
        "rtt",
        re.compile(r"RTT_MONITOR:\s*(\w+)\s*\|\s*RTT:\s*(-?\d+)ms\s*\|\s*Quality:\s*(\d+)"
                   r"\s*\|\s*Using:\s*(\w+)\s*\|\s*Target:\s*([\d.]+)"),
        ["interface", "rtt_ms", "quality", "using", "target"],
    ),
    (
        "rtt",
        re.compile(r"RTT监控:\s*(\w+)\s*=\s*(-?\d+)ms\s*\(质量:(\d+),\s*使用:(\w+),\s*目标:([\d.]+)\)"),
        ["interface", "rtt_ms", "quality", "using", "target"],
    ),
    # 裸的 iface=... 行（server 直接打印的短格式），没有时间戳
    (
        "rtt",
        re.compile(r"iface=(\w+)\s+rtt=(-?\d+)ms\s+quality=(\d+)\s+state=(\d+)"),
        ["interface", "rtt_ms", "quality", "state"],
    ),
    (
        "tcp_loss",
        re.compile(r"TCP_LOSS_MONITOR:\s*interface=(\w+)\s+rate=([\d.]+)%"
                   r"\s+delta_sent=(\d+)\s+delta_retrans=(\d+)\s+level=(\w+)"),
        ["interface", "loss_pct", "sent", "retrans", "level"],
    ),
    (
        "tcp_loss",
        re.compile(r"TCP详细:\s*(\w+)\s*=\s*([\d.]+)%\s*\(发送:(\d+),\s*重传:(\d+),\s*等级:(\w+)\)"),
        ["interface", "loss_pct", "sent", "retrans", "level"],
    ),
    (
        "tcp_loss",
        re.compile(r"iface=(\w+)\s+tcp_loss_rate=([\d.]+)\s+tcp_loss_level=(\w+)"),
        ["interface", "loss_pct", "level"],
    ),
    (
        "traffic",
        re.compile(r"TRAFFIC_MONITOR:\s*Total=([\d.]+)MB/s,\s*Flows=(\d+),\s*PPS=(\d+),"
                   r"\s*Interface=(\w+)"),
        ["mbps", "flows", "pps", "interface"],
    ),
    (
        "traffic",
        re.compile(r"流量监控:\s*(\w+)\s*=\s*([\d.]+)MB/s\s*\(连接:(\d+),\s*包/秒:(\d+)\)"),
        ["interface", "mbps", "flows", "pps"],
    ),
    (
        "rssi",
        re.compile(r"RSSI监控:\s*(\w+)\s*=\s*(-?\d+)dBm\s*\(质量:(\d+),\s*使用:(\w+)\)"),
        ["interface", "rssi_dbm", "quality", "using"],
    ),
    (
        "quality",
        re.compile(r"网络质量变化:\s*(\w+)\s*\(分数:\s*([\d.]+),\s*接口:\s*(\w+)\)"),
        ["level", "score", "interface"],
    ),
    (
        "quality",
        re.compile(r"网络质量:\s*(\w+)\s*=\s*(\w+)\s*\(分数:([\d.]+)\)"),
        ["interface", "level", "score"],
    ),
    (
        "summary",
        re.compile(r"ACTIVE:\s*(\w+)\s*\|\s*RTT:\s*(-?\d+)ms\s*\|\s*Quality:\s*(\d+)"
                   r"\s*\|\s*RSSI:\s*(-?\d+)dBm\s*\|\s*TCP Loss:\s*(-?[\d.]+)%"
                   r"\s*\(([^)]*)\)\s*\|\s*Traffic:\s*([\d.]+)MB/s,\s*(\d+) flows,\s*(\d+) pps"),
        ["interface", "rtt_ms", "quality", "rssi_dbm", "loss_pct",
         "loss_level", "mbps", "flows", "pps"],
    ),
    (
        "summary",
        re.compile(r"接口汇总:\s*(\w+)\s*=\s*RTT:(-?\d+)ms,\s*质量:(\d+),\s*RSSI:(-?\d+)dBm,"
                   r"\s*TCP丢包:(-?[\d.]+)%,\s*流量:([\d.]+)MB/s"),
        ["interface", "rtt_ms", "quality", "rssi_dbm", "loss_pct", "mbps"],
    ),
]

# 服务端用来表示"该字段不适用"的哨兵值（见 server/include/net_info.hpp）。
# 直接把 -1000 当成 RSSI 喂给模型，它会理解成"信号极差" —— 必须翻译。
RSSI_NOT_WIFI = -1000
LOSS_UNKNOWN = -1.0
RTT_UNKNOWN = -1


@dataclass
class LogEvent:
    timestamp: str | None
    interface: str
    kind: str
    values: dict[str, Any]
    raw: str


def parse_log_lines(log_text: str) -> list[LogEvent]:
    """把日志文本解析成事件列表。认不出来的行直接跳过，不报错。"""
    events: list[LogEvent] = []
    last_ts: str | None = None

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 抽时间戳：glog 前缀优先，其次方括号。
        # 抽完要把时间戳从 line 里**摘掉**，否则它会同时留在 raw 里，
        # 渲染出的文本就会出现「10:00:30 I20260924 10:00:30.100000 1234
        # net_monitor.cc:88] ...」这种重复 —— glog 那段前缀每行三十多个字符，
        # 一个 80 行的窗口光噪声就是两千多字符，全是白烧的 token。
        if m := _GLOG_PREFIX.match(line):
            last_ts = m.group(1)
            line = line[m.end():]
        elif m := _BRACKET_TIME.search(line):
            last_ts = m.group(1)
            line = line[: m.start()] + line[m.end():]

        for kind, pattern, fields in _LOG_RULES:
            if m := pattern.search(line):
                values = dict(zip(fields, m.groups()))
                iface = values.get("interface", "unknown")
                events.append(
                    LogEvent(
                        timestamp=last_ts,
                        interface=iface,
                        kind=kind,
                        values=values,
                        raw=line.strip(),
                    )
                )
                break  # 一行只归一类，避免 ACTIVE 行同时命中 rtt 规则

    return events


# ---------------------------------------------------------------------------
# 时间窗聚合
# ---------------------------------------------------------------------------


def _to_seconds(ts: str) -> int:
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


def _from_seconds(total: int) -> str:
    total %= 86400
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _norm(value: Any, sentinel: Any) -> Any:
    """哨兵值翻译成 None —— 这是真实的工程量，简历上可以写。"""
    return None if value == sentinel else value


def _render_window(iface: str, window: list[LogEvent], label: str) -> str:
    """把一个时间窗内的事件渲染成检索文本。

    渲染策略（重要）：这个 text 同时要喂给向量模型、BM25 和 LLM，
    所以它必须同时满足两件互相拉扯的事：

      语义密 —— 要有"从 15ms 升到 210ms""信号持续劣化"这种自然语言描述，
                向量模型才抓得住语义；
      字面全 —— 必须原样保留 eth0 / 210ms / 3.2% / POOR 这些 token，
                BM25 才有东西可匹配。

    只做前者 -> BM25 废掉；只做后者 -> 向量废掉。两边都要留。
    """
    by_kind: dict[str, list[LogEvent]] = defaultdict(list)
    for e in window:
        by_kind[e.kind].append(e)

    lines = [f"[网卡 {iface} | 时间窗 {label}]"]

    # --- RTT：描述趋势，而不是罗列数值 ---
    if rtts := by_kind.get("rtt"):
        seq = [_norm(int(e.values["rtt_ms"]), RTT_UNKNOWN) for e in rtts]
        seq = [v for v in seq if v is not None]
        if seq:
            first, last, peak = seq[0], seq[-1], max(seq)
            delta = last - first
            trend = "基本平稳"
            if delta >= 30:
                trend = f"明显升高（恶化），上升 {delta}ms"
            elif delta <= -30:
                trend = f"明显下降（好转），下降 {abs(delta)}ms"
            lines.append(
                f"RTT 往返时延：起点 {first}ms，终点 {last}ms，峰值 {peak}ms，{trend}"
            )
            if peak > 100:
                lines.append("  RTT 超过 100ms 阈值，属于严重延迟")
        levels = [int(e.values["quality"]) for e in rtts if "quality" in e.values]
        if levels and levels[0] != levels[-1]:
            lines.append(f"  质量等级变化：{levels[0]} -> {levels[-1]}")

    # --- TCP 丢包 ---
    if losses := by_kind.get("tcp_loss"):
        seq = [_norm(float(e.values["loss_pct"]), LOSS_UNKNOWN) for e in losses]
        seq = [v for v in seq if v is not None]
        if seq:
            lines.append(
                f"TCP 丢包率：起点 {seq[0]}%，终点 {seq[-1]}%，峰值 {max(seq)}%"
            )
            if max(seq) >= 1.0:
                lines.append("  丢包率超过 1%，达到影响传输效率的程度")
        levels = [e.values.get("level") for e in losses if e.values.get("level")]
        if levels and levels[0] != levels[-1]:
            lines.append(f"  丢包等级变化：{levels[0]} -> {levels[-1]}")
        # 重传数据是丢包的旁证，一起带上
        for e in losses:
            if "sent" in e.values and "retrans" in e.values:
                lines.append(
                    f"  发送 {e.values['sent']} 包，重传 {e.values['retrans']} 包"
                )

    # --- 流量 ---
    if traffic := by_kind.get("traffic"):
        last = traffic[-1].values
        lines.append(
            f"流量：{last.get('mbps')}MB/s，活跃连接 {last.get('flows')} 个，"
            f"包速率 {last.get('pps')}pps"
        )
        flows = [int(e.values["flows"]) for e in traffic if "flows" in e.values]
        if len(flows) >= 2 and flows[0] != flows[-1]:
            lines.append(f"  活跃连接数变化：{flows[0]} -> {flows[-1]}")

    # --- RSSI（WiFi 专有）---
    if rssi_events := by_kind.get("rssi"):
        seq = [_norm(int(e.values["rssi_dbm"]), RSSI_NOT_WIFI) for e in rssi_events]
        seq = [v for v in seq if v is not None]
        if seq:
            lines.append(f"WiFi 信号强度 RSSI：起点 {seq[0]}dBm，终点 {seq[-1]}dBm")
            if seq[-1] <= -80:
                lines.append("  信号强度低于 -80dBm，属于弱信号，会导致速率下降")

    # --- 汇总行（ACTIVE: ...）---
    if summary := by_kind.get("summary"):
        last = summary[-1].values

        # RSSI 的哨兵值是 -1000。它的含义是「这个字段不适用」，
        # 具体有两种情况：非 WiFi 网卡，或者本轮还没采到。
        # **不能断言成"不是 WiFi 网卡"** —— wlan0 明明就是 WiFi 网卡，
        # 说它不知道反而会让模型排除了正确的排查方向。
        # 正确做法是把哨兵值的含义原样交代清楚，让模型自己判断。
        rssi = _norm(int(last.get("rssi_dbm", RSSI_NOT_WIFI)), RSSI_NOT_WIFI)
        if rssi is not None:
            lines.append(f"WiFi 信号强度：{rssi}dBm")
        else:
            lines.append(
                f"RSSI 无读数（原始值 {RSSI_NOT_WIFI}，是该字段的「不适用」标记："
                f"网卡非 WiFi 或本轮未采集到。**不可理解为信号极差**）"
            )

        # 汇总行里也带 RTT 和丢包，同样是 -1 表示未采集到。
        # 只在没有独立指标行时才用汇总行兜底，避免同一个数字渲染两遍。
        if not by_kind.get("rtt"):
            rtt = _norm(int(last.get("rtt_ms", RTT_UNKNOWN)), RTT_UNKNOWN)
            lines.append(
                f"RTT：未采集到数据（原始值 {RTT_UNKNOWN}，非真实延迟）"
                if rtt is None
                else f"RTT 往返时延：{rtt}ms"
            )
        if not by_kind.get("tcp_loss"):
            loss = _norm(float(last.get("loss_pct", LOSS_UNKNOWN)), LOSS_UNKNOWN)
            lines.append(
                f"TCP 丢包率：未采集到数据（原始值 {LOSS_UNKNOWN}，非真实丢包）"
                if loss is None
                else f"TCP 丢包率：{loss}%"
            )
        if not by_kind.get("traffic") and last.get("mbps") is not None:
            lines.append(
                f"流量：{last.get('mbps')}MB/s，活跃连接 {last.get('flows')} 个，"
                f"包速率 {last.get('pps')}pps"
            )

    # --- 质量评分 ---
    if quality := by_kind.get("quality"):
        for e in quality:
            lines.append(
                f"网络质量判定：{e.values.get('level')}（评分 {e.values.get('score')}）"
            )

    # --- 原始行：给 LLM 留证据，也补足 BM25 的字面覆盖 ---
    lines.append("原始日志：")
    for e in window:
        prefix = f"  {e.timestamp} " if e.timestamp else "  "
        lines.append(prefix + e.raw)

    return "\n".join(lines)


def chunk_logs(
    log_text: str, window_seconds: int = 30, max_events_per_chunk: int = 80
) -> list[Chunk]:
    """日志 -> chunk 列表。按 (网卡, 时间窗) 分组。"""
    events = parse_log_lines(log_text)
    if not events:
        return []

    # 分组键：没有时间戳的事件按顺序每 max_events 条切一段，避免全挤进一个 chunk
    groups: dict[tuple[str, str], list[LogEvent]] = defaultdict(list)
    for idx, e in enumerate(events):
        if e.timestamp:
            start = (_to_seconds(e.timestamp) // window_seconds) * window_seconds
            bucket = _from_seconds(start)
            label = f"{bucket}~{_from_seconds(start + window_seconds)}"
        else:
            bucket_idx = idx // max_events_per_chunk
            label = f"第{bucket_idx + 1}段"
        groups[(e.interface, label)].append(e)

    chunks: list[Chunk] = []
    for (iface, label), window in sorted(groups.items()):
        text = _render_window(iface, window, label)
        digest = hashlib.md5(f"log|{iface}|{label}".encode()).hexdigest()[:12]
        chunks.append(
            Chunk(
                chunk_id=f"log_{digest}",
                text=text,
                metadata={
                    "source": "log",
                    "interface": iface,
                    "time_window": label,
                    "event_count": len(window),
                },
            )
        )
    return chunks


# ---------------------------------------------------------------------------
# 知识库切分
# ---------------------------------------------------------------------------


def _render_kb_value(value: Any, indent: str = "") -> list[str]:
    if isinstance(value, dict):
        out: list[str] = []
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                out.append(f"{indent}{k}:")
                out.extend(_render_kb_value(v, indent + "  "))
            else:
                out.append(f"{indent}{k}: {v}")
        return out
    if isinstance(value, list):
        return [f"{indent}- {item}" for item in value]
    return [f"{indent}{value}"]


def chunk_knowledge(knowledge: dict[str, Any]) -> list[Chunk]:
    """知识库 -> chunk 列表。

    切分粒度：knowledge[大类][子项] 作为一个 chunk。
    比如 rtt_analysis 拆成
        rtt_analysis.description      （RTT 是什么、正常范围）
        rtt_analysis.symptoms         （RTT 异常的典型表现）
        rtt_analysis.troubleshooting  （RTT 高怎么排查）

    每个 chunk 是一条完整可用的知识，而不是 800 个字符。
    """
    chunks: list[Chunk] = []

    for category, content in knowledge.items():
        if not isinstance(content, dict):
            continue
        title = content.get("description", category)

        for key, value in content.items():
            if key == "description":
                continue
            if not isinstance(value, (dict, list)):
                continue

            body = "\n".join(_render_kb_value(value))
            # 标题带上类别名：embedding 时 "RTT" 这个词必须在文本里出现，
            # 否则「延迟高怎么办」这种查询匹配不上 symptoms
            text = f"网络知识 · {category} · {key}\n类别说明：{title}\n{body}"

            digest = hashlib.md5(f"kb|{category}|{key}".encode()).hexdigest()[:12]
            chunks.append(
                Chunk(
                    chunk_id=f"kb_{digest}",
                    text=text,
                    metadata={"source": "knowledge", "category": category, "topic": key},
                )
            )

    return chunks


def build_chunks(
    log_text: str | None = None,
    knowledge: dict[str, Any] | None = None,
    window_seconds: int = 30,
) -> list[Chunk]:
    """一次建好全部 chunk。"""
    chunks: list[Chunk] = []
    if knowledge:
        chunks.extend(chunk_knowledge(knowledge))
    if log_text:
        chunks.extend(chunk_logs(log_text, window_seconds=window_seconds))
    return chunks


def _selfcheck() -> None:
    sample = """
I20260924 10:00:30.100000 1234 net_monitor.cc:88] RTT_MONITOR: eth0 | RTT: 15ms | Quality: 1 | Using: YES | Target: 223.5.5.5
I20260924 10:00:32.100000 1234 net_monitor.cc:88] TRAFFIC_MONITOR: Total=2.5MB/s, Flows=15, PPS=1200, Interface=eth0
I20260924 10:00:48.100000 1234 net_monitor.cc:88] RTT_MONITOR: eth0 | RTT: 210ms | Quality: 4 | Using: YES | Target: 223.5.5.5
I20260924 10:00:49.100000 1234 net_monitor.cc:88] TCP_LOSS_MONITOR: interface=eth0 rate=3.2% delta_sent=137 delta_retrans=12 level=poor
I20260924 10:00:50.100000 1234 net_monitor.cc:88] 网络质量变化: POOR (分数: 42.0, 接口: eth0)
I20260924 10:01:20.100000 1234 net_monitor.cc:88] ACTIVE: wlan0 | RTT: -1ms | Quality: 0 | RSSI: -1000dBm | TCP Loss: -1% () | Traffic: 0MB/s, 0 flows, 0 pps
"""
    chunks = chunk_logs(sample, window_seconds=30)
    print(f"切出 {len(chunks)} 个 chunk\n")
    for c in chunks:
        print("=" * 70)
        print(f"id={c.chunk_id}  meta={c.metadata}")
        print(c.text)
        print()


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()
    _selfcheck()
