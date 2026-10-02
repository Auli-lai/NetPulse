#!/usr/bin/env python3
"""
阿里百炼API测试脚本
测试API调用是否正常工作
"""

import os
from openai import OpenAI

def test_dashscope_api():
    """测试阿里百炼API"""
    try:
        # 设置API密钥
        api_key = "YOUR_DASHSCOPE_API_KEY_HERE"  # 请替换为您的阿里百炼API密钥
        os.environ["DASHSCOPE_API_KEY"] = api_key
        
        # 按照官方文档初始化客户端
        client = OpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        
        print("✅ 阿里百炼客户端初始化成功")
        
        # 测试简单调用
        print("🔄 测试API调用...")
        completion = client.chat.completions.create(
            model="qwen-plus",
            messages=[
                {"role": "system", "content": "你是一个专业的网络问题诊断专家。"},
                {"role": "user", "content": "请简单分析一下RTT延迟15ms的网络状况。"}
            ],
            extra_body={"enable_thinking": False}
        )
        
        result = completion.choices[0].message.content
        print("✅ API调用成功!")
        print(f"📋 返回结果:\n{result}")
        
        return True
        
    except Exception as e:
        print(f"❌ API调用失败: {e}")
        return False

if __name__ == "__main__":
    print("🧪 阿里百炼API测试")
    print("=" * 40)
    
    success = test_dashscope_api()
    
    if success:
        print("\n🎉 API测试成功，可以正常使用!")
    else:
        print("\n❌ API测试失败，请检查配置")
