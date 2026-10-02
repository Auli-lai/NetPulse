#!/usr/bin/env python3
"""
交互式网络RAG分析服务
可以直接处理log_capture.py的输出并回答时间点问题
"""

import os
import sys
import subprocess
import threading
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

from local_vector_rag_analyzer import LocalVectorRAGAnalyzer, LogCaptureParser

class InteractiveNetworkRAG:
    """交互式网络RAG分析服务"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.rag_service = LocalVectorRAGAnalyzer(api_key)
        self.parser = LogCaptureParser()
        self.log_buffer = []
        self.is_capturing = False
        self.capture_process = None
    
    def start_log_capture(self, duration: int = 300):
        """启动log_capture.py并捕获输出"""
        print(f"🚀 启动log_capture.py，持续{duration}秒...")
        
        try:
            # 启动log_capture.py进程
            self.capture_process = subprocess.Popen(
                ['python3', 'log_capture.py'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            
            self.is_capturing = True
            
            # 启动日志读取线程
            capture_thread = threading.Thread(target=self._read_logs, args=(duration,))
            capture_thread.daemon = True
            capture_thread.start()
            
            print("✅ log_capture.py已启动")
            return True
            
        except Exception as e:
            print(f"❌ 启动log_capture.py失败: {e}")
            return False
    
    def _read_logs(self, duration: int):
        """读取日志数据"""
        start_time = time.time()
        
        while self.is_capturing and (time.time() - start_time) < duration:
            try:
                if self.capture_process and self.capture_process.poll() is None:
                    line = self.capture_process.stdout.readline()
                    if line:
                        self.log_buffer.append(line.strip())
                else:
                    break
            except Exception as e:
                print(f"读取日志时出错: {e}")
                break
            
            time.sleep(0.1)
        
        self.is_capturing = False
        if self.capture_process:
            self.capture_process.terminate()
        
        print(f"📊 日志捕获完成，共收集{len(self.log_buffer)}条日志")
    
    def stop_log_capture(self):
        """停止日志捕获"""
        self.is_capturing = False
        if self.capture_process:
            self.capture_process.terminate()
        print("⏹️ 日志捕获已停止")
    
    def analyze_time_point(self, time_point: str) -> str:
        """分析特定时间点的网络情况"""
        if not self.log_buffer:
            return "❌ 没有可用的日志数据，请先启动日志捕获"
        
        log_data = '\n'.join(self.log_buffer)
        return self.rag_service.analyze_time_point(log_data, time_point)
    
    def get_available_times(self) -> str:
        """获取可用的时间点"""
        if not self.log_buffer:
            return "❌ 没有可用的日志数据"
        
        log_data = '\n'.join(self.log_buffer)
        return self.rag_service.get_available_times(log_data)
    
    def interactive_mode(self):
        """交互模式"""
        print("🤖 交互式网络RAG分析服务")
        print("=" * 50)
        print("可用命令:")
        print("1 或 capture [duration] - 启动日志捕获 (默认300秒)")
        print("2 或 stop - 停止日志捕获")
        print("3 或 times - 显示可用时间点")
        print("4 或 analyze [time] - 分析特定时间点 (格式: HH:MM:SS)")
        print("5 或 help - 显示帮助")
        print("6 或 quit - 退出")
        print("\n💡 提示: 可以直接输入数字命令，如 '1' 或 '4 00:13:24'")
        print("=" * 50)
        
        while True:
            try:
                command = input("\n🔧 请输入命令: ").strip().split()
                
                if not command:
                    continue
                
                cmd = command[0].lower()
                
                # 支持数字命令
                if cmd == "1" or cmd == "capture":
                    duration = int(command[1]) if len(command) > 1 else 300
                    self.start_log_capture(duration)
                
                elif cmd == "2" or cmd == "stop":
                    self.stop_log_capture()
                
                elif cmd == "3" or cmd == "times":
                    result = self.get_available_times()
                    print(result)
                
                elif cmd == "4" or cmd == "analyze":
                    if len(command) < 2:
                        print("❌ 请提供时间点，格式: HH:MM:SS")
                        print("💡 示例: analyze 00:13:24 或 4 00:13:24")
                        continue
                    time_point = command[1]
                    result = self.analyze_time_point(time_point)
                    print(f"\n📋 分析结果:\n{result}")
                
                elif cmd == "5" or cmd == "help":
                    print("📖 帮助信息:")
                    print("- 1 或 capture [duration]: 启动日志捕获，duration为秒数")
                    print("- 2 或 stop: 停止日志捕获")
                    print("- 3 或 times: 显示可用的时间点")
                    print("- 4 或 analyze HH:MM:SS: 分析特定时间点的网络情况")
                    print("- 5 或 help: 显示帮助")
                    print("- 6 或 quit: 退出程序")
                    print("\n💡 提示: 可以使用数字命令或完整命令名称")
                
                elif cmd == "6" or cmd == "quit" or cmd == "q":
                    if self.is_capturing:
                        self.stop_log_capture()
                    print("👋 再见!")
                    break
                
                else:
                    print("❌ 未知命令，输入help或5查看帮助")
                    print("💡 提示: 可以使用数字命令(1-6)或完整命令名称")
            
            except KeyboardInterrupt:
                print("\n👋 再见!")
                break
            except Exception as e:
                print(f"❌ 执行命令时出错: {e}")

def main():
    """主函数"""
    api_key = "YOUR_DASHSCOPE_API_KEY_HERE"  # 请替换为您的阿里百炼API密钥
    
    if len(sys.argv) > 1:
        # 命令行模式
        rag_service = InteractiveNetworkRAG(api_key)
        
        if sys.argv[1] == "capture":
            duration = int(sys.argv[2]) if len(sys.argv) > 2 else 300
            rag_service.start_log_capture(duration)
            time.sleep(duration)
            
            # 显示可用时间点
            print("\n" + rag_service.get_available_times())
            
            # 分析几个示例时间点
            sample_times = ["00:13:24", "00:13:30", "00:13:50"]
            for time_point in sample_times:
                print(f"\n{'='*60}")
                print(f"🔍 分析时间点: {time_point}")
                print('='*60)
                result = rag_service.analyze_time_point(time_point)
                print(result)
        
        elif sys.argv[1] == "analyze":
            if len(sys.argv) < 3:
                print("用法: python interactive_rag.py analyze <time_point>")
                return
            
            # 使用示例数据进行分析
            rag_service = InteractiveNetworkRAG(api_key)
            
            # 设置示例日志数据
            sample_log_data = """
🎯 [00:13:24] RTT监控: eth0 = 15ms (质量:1, 使用:YES, 目标:223.5.5.5)
📈 [00:13:34] TCP详细: eth0 = 0.5% (发送:137, 重传:0, 等级:good)
🌊 [00:13:24] 流量监控: eth0 = 2.5MB/s (连接:15, 包/秒:1200)
📶 [00:13:24] RSSI监控: wlan0 = -65dBm (质量:2, 使用:NO)
📋 [00:13:24] 接口汇总: eth0 = RTT:15ms, 质量:1, RSSI:-1000dBm, TCP丢包:0.5%, 流量:2.5MB/s
⭐ [00:13:30] 网络质量: eth0 = good (分数:85.5)
🎯 [00:13:50] RTT监控: eth0 = 18ms (质量:1, 使用:YES, 目标:223.5.5.5)
📈 [00:13:55] TCP详细: eth0 = 0.8% (发送:145, 重传:1, 等级:good)
🌊 [00:13:50] 流量监控: eth0 = 3.2MB/s (连接:18, 包/秒:1500)
            """
            
            rag_service.log_buffer = sample_log_data.strip().split('\n')
            
            time_point = sys.argv[2]
            result = rag_service.analyze_time_point(time_point)
            print(f"\n📋 {time_point} 时间点分析结果:\n{result}")
    
    else:
        # 交互模式
        rag_service = InteractiveNetworkRAG(api_key)
        rag_service.interactive_mode()

if __name__ == "__main__":
    main()
