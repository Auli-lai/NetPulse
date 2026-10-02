#!/bin/bash
# RAG系统安装脚本

echo "🚀 安装WEAK_NET RAG系统依赖..."

# 检查Python版本
python3 --version
if [ $? -ne 0 ]; then
    echo "❌ Python3未安装，请先安装Python3"
    exit 1
fi

# 创建虚拟环境（可选）
if [ "$1" = "--venv" ]; then
    echo "📦 创建虚拟环境..."
    python3 -m venv rag_env
    source rag_env/bin/activate
    echo "✅ 虚拟环境已激活"
fi

# 升级pip
echo "⬆️ 升级pip..."
python3 -m pip install --upgrade pip

# 安装依赖
echo "📚 安装Python依赖包..."
pip install -r requirements.txt

# 检查安装
echo "🔍 检查安装状态..."
python3 -c "
import langchain
import openai
import faiss
print('✅ LangChain版本:', langchain.__version__)
print('✅ OpenAI版本:', openai.__version__)
print('✅ FAISS版本:', faiss.__version__)
print('✅ 所有依赖安装成功!')
"

# 创建示例配置文件
echo "📝 创建配置文件..."
cat > config.json << EOF
{
    "openai_api_key": "YOUR_DASHSCOPE_API_KEY_HERE",
    "log_capture_duration": 300,
    "vector_store_path": "./vector_store",
    "knowledge_base_path": "./network_knowledge_base.py"
}
EOF

echo "✅ RAG系统安装完成!"
echo ""
echo "📖 使用方法:"
echo "1. 交互模式: python3 network_diagnosis_tool.py"
echo "2. 命令行模式: python3 network_diagnosis_tool.py capture 300"
echo "3. 分析模式: python3 network_diagnosis_tool.py analyze 00:13:24 00:13:34"
echo ""
echo "🔧 注意事项:"
echo "- 确保weaknet-dbus-server正在运行"
echo "- 确保有网络连接以访问OpenAI API"
echo "- 首次运行可能需要下载模型，请耐心等待"
