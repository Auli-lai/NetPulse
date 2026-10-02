#!/usr/bin/env python3
"""
本地向量库RAG网络分析系统
使用本地嵌入和FAISS向量库，不依赖外部嵌入API
"""

import os
import json
import re
import time
import pickle
import numpy as np
from datetime import datetime
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

try:
    from openai import OpenAI
    from langchain.text_splitter import RecursiveCharacterTextSplitter
    from langchain_community.vectorstores import FAISS
    from langchain.schema import Document
    VECTOR_RAG_AVAILABLE = True
except ImportError as e:
    print(f"⚠️ 向量RAG依赖不可用: {e}")
    print("将使用简化模式")
    VECTOR_RAG_AVAILABLE = False

from network_knowledge_base import get_network_knowledge

@dataclass
class NetworkMetric:
    """网络指标数据结构"""
    timestamp: str
    interface: str
    rtt: Optional[float] = None
    tcp_loss_rate: Optional[float] = None
    traffic_mbps: Optional[float] = None
    rssi: Optional[int] = None
    quality: Optional[int] = None
    using: Optional[bool] = None
    flows: Optional[int] = None
    pps: Optional[int] = None
    level: Optional[str] = None

class SimpleEmbeddings:
    """简单的本地嵌入实现"""
    
    def __init__(self):
        self.dimension = 384  # 使用较小的维度
    
    def embed_documents(self, texts):
        """为文档生成嵌入"""
        embeddings = []
        for text in texts:
            # 使用简单的TF-IDF风格的嵌入
            embedding = self._text_to_embedding(text)
            embeddings.append(embedding)
        return embeddings
    
    def embed_query(self, text):
        """为查询生成嵌入"""
        return self._text_to_embedding(text)
    
    def __call__(self, texts):
        """支持直接调用"""
        if isinstance(texts, str):
            return self.embed_query(texts)
        else:
            return self.embed_documents(texts)
    
    def _text_to_embedding(self, text):
        """将文本转换为嵌入向量"""
        # 简单的词频统计嵌入
        words = text.lower().split()
        word_counts = {}
        for word in words:
            word_counts[word] = word_counts.get(word, 0) + 1
        
        # 创建固定维度的向量
        embedding = np.zeros(self.dimension)
        
        # 使用哈希函数将词汇映射到向量维度
        for word, count in word_counts.items():
            hash_val = hash(word) % self.dimension
            embedding[hash_val] += count
        
        # 归一化
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm
        
        return embedding

class LogCaptureParser:
    """专门解析log_capture.py输出的解析器"""
    
    def __init__(self):
        # log_capture.py输出的正则表达式模式
        self.patterns = {
            # RTT监控: 🎯 [HH:MM:SS] RTT监控: eth0 = 15ms (质量:1, 使用:YES, 目标:223.5.5.5)
            'rtt_monitor': re.compile(r'🎯 \[(\d{2}:\d{2}:\d{2})\] RTT监控: (\w+) = (\d+)ms \(质量:(\d+), 使用:(\w+), 目标:([\d.]+)\)'),
            
            # TCP丢包: 📈 [HH:MM:SS] TCP详细: eth0 = 0.5% (发送:137, 重传:0, 等级:good)
            'tcp_loss': re.compile(r'📈 \[(\d{2}:\d{2}:\d{2})\] TCP详细: (\w+) = ([\d.]+)% \(发送:(\d+), 重传:(\d+), 等级:(\w+)\)'),
            
            # 流量监控: 🌊 [HH:MM:SS] 流量监控: eth0 = 2.5MB/s (连接:15, 包/秒:1200)
            'traffic': re.compile(r'🌊 \[(\d{2}:\d{2}:\d{2})\] 流量监控: (\w+) = ([\d.]+)MB/s \(连接:(\d+), 包/秒:(\d+)\)'),
            
            # RSSI监控: 📶 [HH:MM:SS] RSSI监控: wlan0 = -65dBm (质量:2, 使用:NO)
            'rssi': re.compile(r'📶 \[(\d{2}:\d{2}:\d{2})\] RSSI监控: (\w+) = (-?\d+)dBm \(质量:(\d+), 使用:(\w+)\)'),
            
            # 接口汇总: 📋 [HH:MM:SS] 接口汇总: eth0 = RTT:15ms, 质量:1, RSSI:-1000dBm, TCP丢包:0.5%, 流量:2.5MB/s
            'interface_summary': re.compile(r'📋 \[(\d{2}:\d{2}:\d{2})\] 接口汇总: (\w+) = RTT:(-?\d+)ms, 质量:(\d+), RSSI:(-?\d+)dBm, TCP丢包:(-?[\d.]+)%, 流量:([\d.]+)MB/s'),
            
            # 网络质量: ⭐ [HH:MM:SS] 网络质量: eth0 = good (分数:85.5)
            'network_quality': re.compile(r'⭐ \[(\d{2}:\d{2}:\d{2})\] 网络质量: (\w+) = (\w+) \(分数:([\d.]+)\)'),
        }
    
    def parse_log_capture_output(self, log_data: str) -> List[NetworkMetric]:
        """解析log_capture.py的输出"""
        metrics = []
        lines = log_data.strip().split('\n')
        
        for line in lines:
            if not line.strip():
                continue
                
            # 解析RTT监控
            rtt_match = self.patterns['rtt_monitor'].search(line)
            if rtt_match:
                time_str, interface, rtt, quality, using, target = rtt_match.groups()
                metrics.append(NetworkMetric(
                    timestamp=time_str,
                    interface=interface,
                    rtt=float(rtt),
                    quality=int(quality),
                    using=using == 'YES'
                ))
                continue
            
            # 解析TCP丢包
            tcp_match = self.patterns['tcp_loss'].search(line)
            if tcp_match:
                time_str, interface, rate, sent, retrans, level = tcp_match.groups()
                metrics.append(NetworkMetric(
                    timestamp=time_str,
                    interface=interface,
                    tcp_loss_rate=float(rate),
                    level=level
                ))
                continue
            
            # 解析流量监控
            traffic_match = self.patterns['traffic'].search(line)
            if traffic_match:
                time_str, interface, mbps, flows, pps = traffic_match.groups()
                metrics.append(NetworkMetric(
                    timestamp=time_str,
                    interface=interface,
                    traffic_mbps=float(mbps),
                    flows=int(flows),
                    pps=int(pps)
                ))
                continue
            
            # 解析RSSI监控
            rssi_match = self.patterns['rssi'].search(line)
            if rssi_match:
                time_str, interface, rssi, quality, using = rssi_match.groups()
                metrics.append(NetworkMetric(
                    timestamp=time_str,
                    interface=interface,
                    rssi=int(rssi),
                    quality=int(quality),
                    using=using == 'YES'
                ))
                continue
            
            # 解析接口汇总
            summary_match = self.patterns['interface_summary'].search(line)
            if summary_match:
                time_str, interface, rtt, quality, rssi, tcp_loss, traffic = summary_match.groups()
                metrics.append(NetworkMetric(
                    timestamp=time_str,
                    interface=interface,
                    rtt=float(rtt) if rtt != '-1' else None,
                    quality=int(quality),
                    rssi=int(rssi) if rssi != '-1000' else None,
                    tcp_loss_rate=float(tcp_loss) if tcp_loss != '-1' else None,
                    traffic_mbps=float(traffic)
                ))
                continue
            
            # 解析网络质量
            quality_match = self.patterns['network_quality'].search(line)
            if quality_match:
                time_str, interface, quality_level, score = quality_match.groups()
                metrics.append(NetworkMetric(
                    timestamp=time_str,
                    interface=interface,
                    quality=float(score)
                ))
                continue
        
        return metrics

class LocalVectorRAGAnalyzer:
    """本地向量库RAG网络分析系统"""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = None
        self.vector_store = None
        self.embeddings = SimpleEmbeddings()
        self.parser = LogCaptureParser()
        self.use_vector_rag = False
        self.vector_store_path = "network_knowledge_vectorstore_local"
        
        # 初始化阿里百炼客户端
        try:
            os.environ["DASHSCOPE_API_KEY"] = api_key
            self.client = OpenAI(
                api_key=os.getenv("DASHSCOPE_API_KEY"),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
            print("✅ 阿里百炼API初始化成功")
        except Exception as e:
            print(f"⚠️ 阿里百炼API初始化失败: {e}")
            return
        
        # 初始化本地向量RAG系统
        if VECTOR_RAG_AVAILABLE:
            try:
                self._initialize_vector_store()
                self.use_vector_rag = True
                print("✅ 本地向量RAG系统初始化成功")
            except Exception as e:
                print(f"⚠️ 本地向量RAG系统初始化失败: {e}")
                print("将使用简化模式")
                self.use_vector_rag = False
        else:
            print("⚠️ 向量RAG依赖不可用，使用简化模式")
    
    def _initialize_vector_store(self):
        """初始化向量存储"""
        print("🔨 初始化本地网络知识库向量存储...")
        
        # 检查是否已有向量存储
        if os.path.exists(self.vector_store_path):
            try:
                print("📂 加载现有向量存储...")
                self.vector_store = FAISS.load_local(
                    self.vector_store_path, 
                    self.embeddings
                )
                print("✅ 向量存储加载成功")
                return
            except Exception as e:
                print(f"⚠️ 加载向量存储失败: {e}")
                print("重新构建向量存储...")
        
        # 构建新的向量存储
        self._build_vector_store()
    
    def _build_vector_store(self):
        """构建向量存储"""
        print("🔨 构建本地网络知识库向量存储...")
        
        # 获取网络知识库
        knowledge_base = get_network_knowledge()
        
        # 将知识库转换为文档
        documents = []
        for category, content in knowledge_base.items():
            # 创建文档内容
            doc_content = f"网络分析知识 - {category}\n\n"
            
            if isinstance(content, dict):
                for key, value in content.items():
                    if isinstance(value, dict):
                        doc_content += f"{key}:\n"
                        for sub_key, sub_value in value.items():
                            if isinstance(sub_value, list):
                                doc_content += f"  {sub_key}: {', '.join(map(str, sub_value))}\n"
                            else:
                                doc_content += f"  {sub_key}: {sub_value}\n"
                    elif isinstance(value, list):
                        doc_content += f"{key}: {', '.join(map(str, value))}\n"
                    else:
                        doc_content += f"{key}: {value}\n"
            else:
                doc_content += str(content)
            
            documents.append(Document(
                page_content=doc_content, 
                metadata={"category": category, "type": "knowledge"}
            ))
        
        # 文本分割
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=800,
            chunk_overlap=150,
            length_function=len,
        )
        
        splits = text_splitter.split_documents(documents)
        print(f"📚 知识库分割为 {len(splits)} 个文档块")
        
        # 构建向量存储
        self.vector_store = FAISS.from_documents(splits, self.embeddings)
        
        # 保存向量存储
        try:
            self.vector_store.save_local(self.vector_store_path)
            print("✅ 本地向量存储构建并保存完成")
        except Exception as e:
            print(f"⚠️ 保存向量存储失败: {e}")
            print("向量存储构建完成，但未保存")
    
    def analyze_time_point(self, log_data: str, time_point: str) -> str:
        """使用本地向量RAG分析特定时间点的网络情况"""
        print(f"🔍 本地向量RAG分析时间点: {time_point}")
        
        # 解析日志数据
        metrics = self.parser.parse_log_capture_output(log_data)
        
        # 获取该时间点的指标
        time_metrics = [m for m in metrics if m.timestamp == time_point]
        
        if not time_metrics:
            return f"❌ 未找到时间点 {time_point} 的网络数据"
        
        print(f"📊 找到时间点 {time_point} 的 {len(time_metrics)} 个指标")
        
        # 构建分析问题
        question = self._build_analysis_question(time_point, time_metrics)
        
        # 使用本地向量RAG进行分析
        if self.use_vector_rag and self.vector_store:
            try:
                print("🤖 使用本地向量RAG系统分析...")
                
                # 从向量存储检索相关文档
                relevant_docs = self.vector_store.similarity_search(question, k=4)
                
                # 构建上下文
                context = "\n\n".join([doc.page_content for doc in relevant_docs])
                
                # 使用AI进行分析
                if self.client:
                    full_prompt = f"""作为网络问题诊断专家，请基于以下检索到的网络知识库信息分析网络状况：

检索到的知识库信息：
{context}

{question}

请提供详细的分析报告，包括：
1. 网络状况评估
2. 问题识别
3. 原因分析（基于知识库信息）
4. 解决建议（引用知识库中的解决方案）
5. 分析依据（说明使用了哪些知识库信息）

回答："""

                    completion = self.client.chat.completions.create(
                        model="qwen-plus",
                        messages=[
                            {"role": "system", "content": "你是一个专业的网络问题诊断专家，擅长基于知识库进行网络分析。"},
                            {"role": "user", "content": full_prompt}
                        ],
                        extra_body={"enable_thinking": False}
                    )
                    
                    result = completion.choices[0].message.content
                    
                    # 添加来源信息
                    result += "\n\n📚 向量检索来源：\n"
                    for i, doc in enumerate(relevant_docs, 1):
                        category = doc.metadata.get("category", "未知")
                        content_preview = doc.page_content[:100] + "..." if len(doc.page_content) > 100 else doc.page_content
                        result += f"{i}. {category}: {content_preview}\n"
                    
                    print("✅ 本地向量RAG分析完成")
                    return result
                else:
                    return self._fallback_analysis(time_point, time_metrics, context)
                
            except Exception as e:
                print(f"⚠️ 本地向量RAG分析失败: {e}")
                return self._fallback_analysis(time_point, time_metrics)
        else:
            return self._fallback_analysis(time_point, time_metrics)
    
    def _build_analysis_question(self, time_point: str, metrics: List[NetworkMetric]) -> str:
        """构建分析问题"""
        question = f"请分析在 {time_point} 时间点的网络情况。\n\n"
        question += "网络指标数据：\n"
        
        # 按接口分组指标
        interface_metrics = {}
        for metric in metrics:
            if metric.interface not in interface_metrics:
                interface_metrics[metric.interface] = []
            interface_metrics[metric.interface].append(metric)
        
        for interface, iface_metrics in interface_metrics.items():
            question += f"\n接口 {interface}:\n"
            
            for metric in iface_metrics:
                question += f"- 时间: {metric.timestamp}\n"
                if metric.rtt is not None:
                    question += f"  RTT延迟: {metric.rtt}ms\n"
                if metric.tcp_loss_rate is not None:
                    question += f"  TCP丢包率: {metric.tcp_loss_rate}%\n"
                if metric.traffic_mbps is not None:
                    question += f"  网络流量: {metric.traffic_mbps}MB/s\n"
                if metric.rssi is not None:
                    question += f"  WiFi信号: {metric.rssi}dBm\n"
                if metric.quality is not None:
                    question += f"  质量评分: {metric.quality}\n"
                if metric.flows is not None:
                    question += f"  活跃连接: {metric.flows}\n"
                if metric.pps is not None:
                    question += f"  包速率: {metric.pps} pps\n"
                if metric.level is not None:
                    question += f"  丢包等级: {metric.level}\n"
                question += "\n"
        
        return question
    
    def _fallback_analysis(self, time_point: str, metrics: List[NetworkMetric], context: str = "") -> str:
        """备用分析（当向量RAG不可用时）"""
        print("📊 使用备用分析模式...")
        
        # 简单的本地分析
        analysis = f"🔍 {time_point} 时间点网络情况分析（备用模式）\n"
        analysis += "=" * 60 + "\n"
        
        # 按接口分组
        interface_metrics = {}
        for metric in metrics:
            if metric.interface not in interface_metrics:
                interface_metrics[metric.interface] = []
            interface_metrics[metric.interface].append(metric)
        
        for interface, iface_metrics in interface_metrics.items():
            analysis += f"\n📡 接口 {interface} 分析:\n"
            
            # 统计指标
            rtt_values = [m.rtt for m in iface_metrics if m.rtt is not None]
            tcp_loss_values = [m.tcp_loss_rate for m in iface_metrics if m.tcp_loss_rate is not None]
            traffic_values = [m.traffic_mbps for m in iface_metrics if m.traffic_mbps is not None]
            rssi_values = [m.rssi for m in iface_metrics if m.rssi is not None]
            
            if rtt_values:
                avg_rtt = sum(rtt_values) / len(rtt_values)
                status = "🟢 优秀" if avg_rtt <= 10 else "🟡 良好" if avg_rtt <= 30 else "🟠 一般" if avg_rtt <= 50 else "🔴 较差"
                analysis += f"  • RTT延迟: {avg_rtt:.1f}ms {status}\n"
            
            if tcp_loss_values:
                avg_loss = sum(tcp_loss_values) / len(tcp_loss_values)
                status = "🟢 优秀" if avg_loss <= 0.5 else "🟡 良好" if avg_loss <= 1 else "🟠 一般" if avg_loss <= 3 else "🔴 较差"
                analysis += f"  • TCP丢包率: {avg_loss:.2f}% {status}\n"
            
            if traffic_values:
                avg_traffic = sum(traffic_values) / len(traffic_values)
                status = "🟢 正常" if avg_traffic > 0 else "🔴 异常"
                analysis += f"  • 网络流量: {avg_traffic:.1f}MB/s {status}\n"
            
            if rssi_values:
                avg_rssi = sum(rssi_values) / len(rssi_values)
                status = "🟢 优秀" if avg_rssi >= -50 else "🟡 良好" if avg_rssi >= -60 else "🟠 一般" if avg_rssi >= -70 else "🔴 较差"
                analysis += f"  • WiFi信号: {avg_rssi:.0f}dBm {status}\n"
        
        if context:
            analysis += f"\n📚 检索到的知识库信息:\n{context[:200]}...\n"
        
        analysis += f"\n💡 建议: 启用本地向量RAG系统可获得更详细的分析报告"
        
        return analysis
    
    def get_available_times(self, log_data: str) -> str:
        """获取可用的时间点"""
        metrics = self.parser.parse_log_capture_output(log_data)
        
        # 获取所有唯一的时间点
        times = sorted(set(metric.timestamp for metric in metrics))
        
        summary = []
        summary.append("📅 可用时间点:")
        summary.append("=" * 30)
        
        for time_str in times:
            count = len([m for m in metrics if m.timestamp == time_str])
            summary.append(f"• {time_str}: {count} 个指标")
        
        return "\n".join(summary)
    
    def search_knowledge(self, query: str) -> str:
        """搜索知识库"""
        if not self.use_vector_rag or not self.vector_store:
            return "❌ 本地向量RAG系统不可用"
        
        try:
            print(f"🔍 搜索知识库: {query}")
            
            # 使用向量存储搜索
            docs = self.vector_store.similarity_search(query, k=3)
            
            result = f"📚 知识库搜索结果 (查询: {query}):\n"
            result += "=" * 50 + "\n"
            
            for i, doc in enumerate(docs, 1):
                category = doc.metadata.get("category", "未知")
                content = doc.page_content[:200] + "..." if len(doc.page_content) > 200 else doc.page_content
                result += f"\n{i}. {category}:\n{content}\n"
            
            return result
            
        except Exception as e:
            return f"❌ 搜索失败: {e}"

def main():
    """主函数 - 示例用法"""
    # 初始化本地向量RAG系统
    api_key = "YOUR_DASHSCOPE_API_KEY_HERE"  # 请替换为您的阿里百炼API密钥
    rag_analyzer = LocalVectorRAGAnalyzer(api_key)
    
    # 示例log_capture.py输出
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
    
    print("🚀 本地向量库RAG网络分析系统")
    print("=" * 60)
    
    # 显示可用时间点
    print("\n" + rag_analyzer.get_available_times(sample_log_data))
    
    # 测试知识库搜索
    print(f"\n{'='*60}")
    print("🔍 测试知识库搜索")
    print('='*60)
    search_result = rag_analyzer.search_knowledge("RTT延迟分析")
    print(search_result)
    
    # 分析特定时间点
    time_points = ["00:13:24", "00:13:30", "00:13:50"]
    
    for time_point in time_points:
        print(f"\n{'='*60}")
        print(f"🔍 本地向量RAG分析时间点: {time_point}")
        print('='*60)
        
        result = rag_analyzer.analyze_time_point(sample_log_data, time_point)
        print(result)

if __name__ == "__main__":
    main()
