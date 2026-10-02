#!/usr/bin/env python3
"""
网络分析知识库
包含网络监控指标的含义、正常范围、异常情况分析等知识
"""

NETWORK_KNOWLEDGE_BASE = {
    "rtt_analysis": {
        "description": "RTT (Round Trip Time) 往返时延分析",
        "normal_range": "1-50ms",
        "excellent": "1-10ms",
        "good": "10-30ms", 
        "fair": "30-50ms",
        "poor": "50-100ms",
        "critical": ">100ms",
        "symptoms": {
            "high_rtt": "RTT过高会导致网络延迟增加，影响实时应用性能",
            "rtt_timeout": "RTT超时(-1ms)表示网络连接失败或目标不可达",
            "rtt_fluctuation": "RTT波动大表示网络不稳定"
        },
        "troubleshooting": {
            "high_rtt": "检查网络拥塞、路由问题、DNS解析延迟",
            "timeout": "检查网络连接、防火墙设置、目标服务器状态",
            "fluctuation": "检查网络质量、干扰源、负载均衡配置"
        }
    },
    
    "tcp_loss_analysis": {
        "description": "TCP丢包率分析",
        "normal_range": "0-1%",
        "excellent": "0%",
        "good": "0-0.5%",
        "fair": "0.5-1%",
        "poor": "1-3%",
        "critical": ">3%",
        "symptoms": {
            "high_loss": "丢包率高会导致数据传输重传，降低网络效率",
            "burst_loss": "突发丢包可能表示网络拥塞或设备故障",
            "persistent_loss": "持续丢包可能表示链路质量问题"
        },
        "troubleshooting": {
            "high_loss": "检查网络设备、链路质量、拥塞控制",
            "burst_loss": "检查网络设备状态、流量突发、QoS配置",
            "persistent_loss": "检查物理链路、设备端口、网络配置"
        }
    },
    
    "traffic_analysis": {
        "description": "网络流量分析",
        "normal_indicators": {
            "bandwidth_utilization": "正常利用率应<80%",
            "flow_count": "活跃连接数应合理",
            "packet_rate": "包速率应稳定"
        },
        "symptoms": {
            "zero_traffic": "流量为0可能表示网络中断或监控问题",
            "high_utilization": "高利用率可能导致网络拥塞",
            "abnormal_flows": "异常连接数可能表示攻击或配置问题"
        },
        "troubleshooting": {
            "zero_traffic": "检查网络连接、eBPF程序、监控配置",
            "high_utilization": "检查带宽限制、流量控制、网络规划",
            "abnormal_flows": "检查安全策略、连接限制、异常检测"
        }
    },
    
    "rssi_analysis": {
        "description": "WiFi信号强度分析",
        "signal_levels": {
            "excellent": "-30 to -50 dBm",
            "good": "-50 to -60 dBm",
            "fair": "-60 to -70 dBm",
            "poor": "-70 to -80 dBm",
            "critical": "< -80 dBm"
        },
        "symptoms": {
            "low_rssi": "信号弱会导致连接不稳定、速度慢",
            "rssi_fluctuation": "信号波动表示环境干扰或设备问题",
            "no_signal": "无信号表示WiFi未连接或设备故障"
        },
        "troubleshooting": {
            "low_rssi": "检查距离、障碍物、天线方向、功率设置",
            "fluctuation": "检查干扰源、设备稳定性、环境变化",
            "no_signal": "检查WiFi配置、设备状态、网络连接"
        }
    },
    
    "interface_analysis": {
        "description": "网络接口状态分析",
        "states": {
            "up": "接口正常，可以传输数据",
            "down": "接口关闭，无法传输数据",
            "unknown": "接口状态未知"
        },
        "symptoms": {
            "interface_down": "接口关闭会导致网络中断",
            "no_active_interface": "无活跃接口表示网络完全中断",
            "interface_changes": "接口频繁变化表示网络不稳定"
        },
        "troubleshooting": {
            "interface_down": "检查物理连接、驱动状态、配置错误",
            "no_active_interface": "检查网络配置、路由设置、服务状态",
            "interface_changes": "检查网络稳定性、配置冲突、设备故障"
        }
    },
    
    "quality_assessment": {
        "description": "网络质量综合评估",
        "quality_levels": {
            "excellent": "所有指标优秀，网络性能最佳",
            "good": "大部分指标良好，网络性能良好",
            "fair": "部分指标一般，网络性能可接受",
            "poor": "多个指标较差，网络性能不佳",
            "critical": "关键指标严重，网络性能极差"
        },
        "assessment_factors": [
            "RTT延迟",
            "TCP丢包率", 
            "流量稳定性",
            "信号强度",
            "接口状态"
        ]
    },
    
    "common_issues": {
        "network_congestion": {
            "symptoms": ["高RTT", "高丢包率", "流量异常"],
            "causes": ["带宽不足", "设备过载", "配置问题"],
            "solutions": ["增加带宽", "优化配置", "负载均衡"]
        },
        "hardware_failure": {
            "symptoms": ["接口down", "信号丢失", "流量中断"],
            "causes": ["设备故障", "线缆问题", "端口损坏"],
            "solutions": ["更换设备", "检查线缆", "更换端口"]
        },
        "configuration_error": {
            "symptoms": ["连接失败", "性能异常", "状态错误"],
            "causes": ["配置错误", "参数不当", "策略冲突"],
            "solutions": ["检查配置", "修正参数", "解决冲突"]
        },
        "environmental_interference": {
            "symptoms": ["信号波动", "连接不稳定", "性能下降"],
            "causes": ["电磁干扰", "物理障碍", "距离过远"],
            "solutions": ["消除干扰", "调整位置", "增强信号"]
        }
    }
}

def get_network_knowledge():
    """获取网络分析知识库"""
    return NETWORK_KNOWLEDGE_BASE

def analyze_metric(metric_type, value, context=""):
    """分析特定网络指标"""
    knowledge = NETWORK_KNOWLEDGE_BASE.get(metric_type, {})
    
    if not knowledge:
        return f"未知指标类型: {metric_type}"
    
    analysis = {
        "metric": metric_type,
        "value": value,
        "context": context,
        "analysis": knowledge
    }
    
    return analysis
