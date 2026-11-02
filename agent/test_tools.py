"""
工具层冒烟测试 —— 确认 Python 能真正调通 C++ 服务。

    python3 test_tools.py

跑之前：
    1) 先启动 server（注意 sudo -E，见 README 第二节）
    2) sudo apt install python3-dbus

===========================================================================
和 test_agent.py 的分工
===========================================================================

    test_agent.py   测**循环**：容错、重复检测、轮次上限、上下文裁剪。
                    全离线，不需要任何外部依赖。改循环逻辑后跑这个。

    test_tools.py   测**接线**：D-Bus 通不通、每个工具真实返回什么、
                    服务端有没有重新编译（GetFlows 是后加的）。
                    需要 server 在跑。改工具或改 C++ 后跑这个。

分开是因为它们的失败原因完全不同：前者错了是逻辑问题，
后者错了是环境问题。混在一个文件里，报错时你得先判断是哪一类。
"""

from __future__ import annotations

import json
import os
import sys

from netpulse_client import NetPulseClient, NetPulseError
from tools import TOOL_NAMES, ToolRunner


def main() -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rag"))
    from console import enable_utf8_output

    enable_utf8_output()

    print(f"已注册工具：{TOOL_NAMES}\n")

    # ---- 1. 连通性 ----
    # 刻意不传 client，让 ToolRunner 自己懒加载，这样测的就是真实路径
    try:
        client = NetPulseClient()
    except NetPulseError as exc:
        print("[FAIL] 连不上服务\n")
        print(exc)
        return 1
    print("[OK] D-Bus 连接成功\n")

    runner = ToolRunner(client)
    failures = 0

    # ---- 2. 逐个工具 ----
    # 注意 get_flows 会阻塞约 1 秒（底层要采两次快照求速率），所以放最后
    cases = [
        ("list_interfaces", {}),
        ("get_network_health", {}),
        ("get_conn_stats", {}),
        ("get_conn_stats", {"interface": "wlan0"}),
        ("ping_host", {"hostname": "223.5.5.5"}),
        ("get_flows", {"top_n": 5}),
    ]

    for name, args in cases:
        result = runner.run(name, args)
        ok = "error" not in result
        print(f"[{'OK' if ok else 'FAIL'}] {name}({json.dumps(args, ensure_ascii=False)})")
        print(json.dumps(result, ensure_ascii=False, indent=2)[:1200])
        print()
        failures += 0 if ok else 1

    # ---- 3. 错误路径：必须被吞掉并回灌，而不是崩 ----
    print("--- 错误路径检查（这些都应该返回 dict，绝不能抛异常）---")
    checks = [
        ("未知工具", "no_such_tool", {}),
        ("错误参数名", "ping_host", {"wrong_arg": 1}),
        ("缺必填项", "ping_host", {}),
        ("目标不通", "ping_host", {"hostname": "192.0.2.1"}),
    ]
    for label, name, args in checks:
        result = runner.run(name, args)
        is_dict = isinstance(result, dict)
        print(f"  {'OK  ' if is_dict else 'FAIL'} {label} -> "
              f"{json.dumps(result, ensure_ascii=False)[:140]}")
        failures += 0 if is_dict else 1
    print()

    # ---- 4. GetFlows 专项：老服务端会缺这个方法 ----
    print("--- GetFlows 专项检查 ---")
    flows = runner.run("get_flows", {"top_n": 3})
    if "error" in flows:
        print("  ⚠️  get_flows 失败。如果是 UnknownMethod，说明 C++ 服务还没重新编译。")
        print(f"  {json.dumps(flows, ensure_ascii=False)[:400]}")
    else:
        print(f"  OK  采集到 {flows.get('flow_count', 0)} 条连接")
        top = (flows.get("top_by_bps") or [{}])[0]
        print(f"  最活跃的一条：{json.dumps(top, ensure_ascii=False)[:200]}")
    print()

    # ---- 5. schema 自检 ----
    print("--- schema 自检 ---")
    for tool in runner.tools:
        spec = tool.parameters
        assert tool.name, "工具必须有名字"
        assert len(tool.description) > 50, f"{tool.name} 的 description 太短"
        assert spec.get("type") == "object", f"{tool.name} 的 parameters 必须是 object"
        props = set(spec.get("properties", {}))
        required = set(spec.get("required", []))
        assert required <= props, f"{tool.name}: required 里有 properties 没声明的字段"
        sig = ", ".join(sorted(props))
        print(f"  {tool.name:24s} 参数=[{sig}]")
    print()

    print(f"完成。失败 {failures} 项。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
