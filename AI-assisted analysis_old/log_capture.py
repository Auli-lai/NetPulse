#!/usr/bin/env python3
"""
WEAK_NET 日志截取器
专门截取weaknet-dbus-server的网络指标日志，不做分析功能
"""

import re
import subprocess
import time
from datetime import datetime

def capture_network_logs():
    """截取网络指标日志"""
    print("🚀 启动WEAK_NET服务器...")
    
    try:
        # 启动服务器进程，设置工作目录为server/bin目录
        process = subprocess.Popen(
            ['./weaknet-dbus-server'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
            cwd='../server/bin'  # 设置工作目录为server/bin
        )
        
        print("✅ 服务器已启动，开始截取网络指标日志...")
        print("按Ctrl+C停止")
        print("="*80)
        
        # 网络指标相关的正则表达式模式
        patterns = {
            # RTT信息: iface=eth0 rtt=1ms quality=1 state=1
            'rtt': re.compile(r'iface=(\w+) rtt=(\d+)ms quality=(\d+) state=(\d+)'),
            
            # RTT监控: RTT_MONITOR: eth0 | RTT: 1ms | Quality: 1 | Using: YES | Target: 223.5.5.5
            'rtt_monitor': re.compile(r'RTT_MONITOR: (\w+) \| RTT: (\d+)ms \| Quality: (\d+) \| Using: (\w+) \| Target: ([\d.]+)'),
            
            # TCP丢包率: iface=eth0 tcp_loss_rate=0 tcp_loss_level=good
            'tcp_loss': re.compile(r'iface=(\w+) tcp_loss_rate=([\d.]+) tcp_loss_level=(\w+)'),
            
            # TCP丢包详细: TCP_LOSS_MONITOR: interface=eth0 rate=0% delta_sent=325 delta_retrans=0 level=good
            'tcp_loss_detailed': re.compile(r'TCP_LOSS_MONITOR: interface=(\w+) rate=([\d.]+)% delta_sent=(\d+) delta_retrans=(\d+) level=(\w+)'),
            
            # 流量监控: TRAFFIC_MONITOR: Total=0MB/s, Flows=0, PPS=0, Interface=eth0
            'traffic': re.compile(r'TRAFFIC_MONITOR: Total=([\d.]+)MB/s, Flows=(\d+), PPS=(\d+), Interface=(\w+)'),
            
            # 网络质量: 网络质量变化: POOR (分数: 42.0, 接口: eth0)
            'network_quality': re.compile(r'网络质量变化: (\w+) \(分数: ([\d.]+), 接口: (\w+)\)'),
            
            # 网络质量稳定: 网络质量稳定: UNKNOWN ( 分数: 0.0)
            'network_quality_stable': re.compile(r'网络质量稳定: (\w+) \( 分数: ([\d.]+)\)'),
            
            # 无变化检测: RTT_MONITOR: no changes detected (interfaces: X)
            'no_changes_rtt': re.compile(r'RTT_MONITOR: no changes detected \(interfaces: (\d+)\)'),
            
            # 无变化检测: RSSI_MONITOR: no changes detected (interfaces: X)
            'no_changes_rssi': re.compile(r'RSSI_MONITOR: no changes detected \(interfaces: (\d+)\)'),
            
            # 无活跃接口: TCP_LOSS_MONITOR: no active interface found
            'no_active_interface': re.compile(r'TCP_LOSS_MONITOR: no active interface found'),
            
            # RSSI tick: tick: updating RSSI for X ifaces
            'rssi_tick': re.compile(r'tick: updating RSSI for (\d+) ifaces'),
            
            # 接口状态: current=eth0 flags=1
            'interface_status': re.compile(r'current=(\w+) flags=(\d+)'),
            
            # 接口汇总: ACTIVE: eth0 | RTT: -1ms | Quality: 0 | RSSI: -1000dBm | TCP Loss: -1% () | Traffic: 0MB/s, 0 flows, 0 pps
            'interface_summary': re.compile(r'ACTIVE: (\w+) \| RTT: (-?\d+)ms \| Quality: (\d+) \| RSSI: (-?\d+)dBm \| TCP Loss: (-?[\d.]+)% \(([^)]*)\) \| Traffic: ([\d.]+)MB/s, (\d+) flows, (\d+) pps'),
            
            # 接口不活跃: INACTIVE: eth0 | RTT: -1ms | Quality: 0 | RSSI: -1000dBm | TCP Loss: -1% ()
            'interface_inactive': re.compile(r'INACTIVE: (\w+) \| RTT: (-?\d+)ms \| Quality: (\d+) \| RSSI: (-?\d+)dBm \| TCP Loss: (-?[\d.]+)% \(([^)]*)\)'),
            
            # 接口收集: collected 1 interfaces
            'interface_collected': re.compile(r'collected (\d+) interfaces'),
            
            # 接口变化: Interfaces changed (using flags in log): +eth0 -
            'interface_changed': re.compile(r'Interfaces changed \(using flags in log\): ([+-]\w+(?: [+-]\w+)*)'),
            
            # 监控线程启动: monitor thread started
            'monitor_started': re.compile(r'(\w+) monitor thread started'),
            
            # 接口tick: tick: collecting interfaces...
            'interface_tick': re.compile(r'tick: collecting interfaces'),
            
            # eBPF相关日志
            'ebpf_loading': re.compile(r'libbpf: loading (.*\.bpf\.o)'),
            'ebpf_program': re.compile(r'libbpf: prog \'(\w+)\': found program \'(\w+)\' at insn offset (\d+)'),
            'ebpf_map': re.compile(r'libbpf: map \'(\w+)\': created successfully, fd=(\d+)'),
            'traffic_analyzer_start': re.compile(r'Traffic analyzer started for interface: (\w+) \(interval=(\d+)s\)'),
            'traffic_analysis_start': re.compile(r'Traffic analysis loop started'),
        }
        
        # 读取输出
        line_count = 0
        while True:
            line = process.stdout.readline()
            if line:
                line_count += 1
                timestamp = datetime.now().strftime("%H:%M:%S")

                # 调试：显示前10行输出
                if line_count <= 10:
                    print(f"🔍 调试[{line_count}]: {line.strip()}")

                # 检查是否包含网络指标关键词 - 扩大匹配范围
                if any(keyword in line for keyword in [
                    'iface=', 'RTT_MONITOR', 'TRAFFIC_MONITOR', '网络质量变化',
                    'TCP_LOSS_MONITOR', 'current=', 'ACTIVE:', 'libbpf:',
                    'Traffic analyzer', 'Traffic analysis', 'RTT:', 'Quality:',
                    'RSSI:', 'TCP Loss:', 'interface=', 'rtt=', 'quality=',
                    'state=', 'flags=', 'tcp_loss_rate=', 'tcp_loss_level=',
                    '网络质量稳定', '网络质量变化', 'monitor thread', 'tick:',
                    'collected', 'interfaces', 'INACTIVE:', 'ACTIVE:',
                    'attach', 'failed', 'error', 'errno', 'kprobe', 'fentry'
                ]):
                    # 解析并显示RTT信息
                    rtt_match = patterns['rtt'].search(line)
                    if rtt_match:
                        interface, rtt, quality, state = rtt_match.groups()
                        print(f"🔗 [{timestamp}] RTT: {interface} = {rtt}ms (质量:{quality}, 状态:{state})")
                        continue
                    
                    # 解析并显示RTT监控
                    rtt_monitor_match = patterns['rtt_monitor'].search(line)
                    if rtt_monitor_match:
                        interface, rtt, quality, using, target = rtt_monitor_match.groups()
                        print(f"🎯 [{timestamp}] RTT监控: {interface} = {rtt}ms (质量:{quality}, 使用:{using}, 目标:{target})")
                        continue
                    
                    # 解析并显示TCP丢包率
                    tcp_match = patterns['tcp_loss'].search(line)
                    if tcp_match:
                        interface, rate, level = tcp_match.groups()
                        print(f"📊 [{timestamp}] TCP丢包: {interface} = {rate}% ({level})")
                        continue
                    
                    # 解析并显示TCP丢包详细
                    tcp_detailed_match = patterns['tcp_loss_detailed'].search(line)
                    if tcp_detailed_match:
                        interface, rate, delta_sent, delta_retrans, level = tcp_detailed_match.groups()
                        print(f"📈 [{timestamp}] TCP详细: {interface} = {rate}% (发送:{delta_sent}, 重传:{delta_retrans}, 等级:{level})")
                        continue
                    
                    # 解析并显示流量监控
                    traffic_match = patterns['traffic'].search(line)
                    if traffic_match:
                        total_mbps, flows, pps, interface = traffic_match.groups()
                        print(f"🌊 [{timestamp}] 流量监控: {interface} = {total_mbps}MB/s (连接:{flows}, 包/秒:{pps})")
                        continue
                    
                    # 解析并显示网络质量
                    quality_match = patterns['network_quality'].search(line)
                    if quality_match:
                        quality, score, interface = quality_match.groups()
                        print(f"⭐ [{timestamp}] 网络质量: {interface} = {quality} (分数:{score})")
                        continue
                    
                    # 解析并显示网络质量稳定
                    quality_stable_match = patterns['network_quality_stable'].search(line)
                    if quality_stable_match:
                        quality, score = quality_stable_match.groups()
                        print(f"🔒 [{timestamp}] 网络质量稳定: {quality} (分数:{score})")
                        continue
                    
                    # 解析并显示接口状态
                    status_match = patterns['interface_status'].search(line)
                    if status_match:
                        interface, flags = status_match.groups()
                        print(f"🔌 [{timestamp}] 接口状态: {interface} = flags:{flags}")
                        continue
                    
                    # 解析并显示接口汇总
                    interface_summary_match = patterns['interface_summary'].search(line)
                    if interface_summary_match:
                        interface, rtt, quality, rssi, tcp_loss, tcp_loss_desc, traffic, flows, pps = interface_summary_match.groups()
                        print(f"📋 [{timestamp}] 接口汇总: {interface} = RTT:{rtt}ms, 质量:{quality}, RSSI:{rssi}dBm, TCP丢包:{tcp_loss}%, 流量:{traffic}MB/s")
                        continue
                    
                    # 解析并显示接口不活跃状态
                    interface_inactive_match = patterns['interface_inactive'].search(line)
                    if interface_inactive_match:
                        interface, rtt, quality, rssi, tcp_loss, tcp_loss_desc = interface_inactive_match.groups()
                        print(f"🔴 [{timestamp}] 接口不活跃: {interface} = RTT:{rtt}ms, 质量:{quality}, RSSI:{rssi}dBm, TCP丢包:{tcp_loss}%")
                        continue
                    
                    # 解析并显示接口收集信息
                    interface_collected_match = patterns['interface_collected'].search(line)
                    if interface_collected_match:
                        count = interface_collected_match.group(1)
                        print(f"📊 [{timestamp}] 接口收集: 发现{count}个接口")
                        continue
                    
                    # 解析并显示接口变化
                    interface_changed_match = patterns['interface_changed'].search(line)
                    if interface_changed_match:
                        changes = interface_changed_match.group(1)
                        print(f"🔄 [{timestamp}] 接口变化: {changes}")
                        continue
                    
                    # 解析并显示监控线程启动
                    monitor_started_match = patterns['monitor_started'].search(line)
                    if monitor_started_match:
                        monitor_type = monitor_started_match.group(1)
                        print(f"🚀 [{timestamp}] 监控线程: {monitor_type} 已启动")
                        continue
                    
                    # 解析并显示接口tick
                    interface_tick_match = patterns['interface_tick'].search(line)
                    if interface_tick_match:
                        print(f"⏰ [{timestamp}] 接口检查: 正在收集接口信息...")
                        continue
                    
                    # 解析并显示RTT无变化
                    no_changes_rtt_match = patterns['no_changes_rtt'].search(line)
                    if no_changes_rtt_match:
                        count = no_changes_rtt_match.group(1)
                        print(f"🔍 [{timestamp}] RTT监控: 无变化检测 (接口数:{count})")
                        continue
                    
                    # 解析并显示RSSI无变化
                    no_changes_rssi_match = patterns['no_changes_rssi'].search(line)
                    if no_changes_rssi_match:
                        count = no_changes_rssi_match.group(1)
                        print(f"📶 [{timestamp}] RSSI监控: 无变化检测 (接口数:{count})")
                        continue
                    
                    # 解析并显示无活跃接口
                    no_active_interface_match = patterns['no_active_interface'].search(line)
                    if no_active_interface_match:
                        print(f"⚠️  [{timestamp}] TCP丢包监控: 无活跃接口")
                        continue
                    
                    # 解析并显示RSSI tick
                    rssi_tick_match = patterns['rssi_tick'].search(line)
                    if rssi_tick_match:
                        count = rssi_tick_match.group(1)
                        print(f"📶 [{timestamp}] RSSI检查: 更新{count}个接口")
                        continue
                    
                    # 解析并显示eBPF相关日志
                    ebpf_loading_match = patterns['ebpf_loading'].search(line)
                    if ebpf_loading_match:
                        bpf_file = ebpf_loading_match.group(1)
                        print(f"🔧 [{timestamp}] eBPF加载: {bpf_file}")
                        continue

                    ebpf_program_match = patterns['ebpf_program'].search(line)
                    if ebpf_program_match:
                        prog_name, func_name, offset = ebpf_program_match.groups()
                        print(f"⚙️  [{timestamp}] eBPF程序: {prog_name} -> {func_name} (偏移:{offset})")
                        continue

                    ebpf_map_match = patterns['ebpf_map'].search(line)
                    if ebpf_map_match:
                        map_name, fd = ebpf_map_match.groups()
                        print(f"🗺️  [{timestamp}] eBPF映射: {map_name} (fd:{fd})")
                        continue

                    traffic_analyzer_match = patterns['traffic_analyzer_start'].search(line)
                    if traffic_analyzer_match:
                        interface, interval = traffic_analyzer_match.groups()
                        print(f"🚀 [{timestamp}] 流量分析器启动: {interface} (间隔:{interval}s)")
                        continue

                    traffic_analysis_match = patterns['traffic_analysis_start'].search(line)
                    if traffic_analysis_match:
                        print(f"🔄 [{timestamp}] 流量分析循环启动")
                        continue

                    # 检查eBPF附加错误
                    if 'attach' in line and ('failed' in line or 'error' in line):
                        print(f"❌ [{timestamp}] eBPF附加错误: {line.strip()}")
                        continue
                    
                    # 其他包含关键词但未匹配的行
                    print(f"📝 [{timestamp}] 其他: {line.strip()}")

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n⏹️ 日志截取已停止")
    except Exception as e:
        print(f"❌ 错误: {e}")
    finally:
        if 'process' in locals():
            process.terminate()
            process.wait()

if __name__ == "__main__":
    capture_network_logs()