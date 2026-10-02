# NetPulse Web 服务
#
# ⚠️ 构建上下文是**项目根目录**，不是 web/，因为要拷 agent/ 和 rag/。
#    用 compose 会自动处理；手动构建的话：
#
#        cd "AI-Network-Monitoring-System"
#        docker build -f web/Dockerfile -t netpulse-web .

FROM python:3.12-slim

# faiss-cpu 依赖 libgomp（OpenMP 运行时），python:slim 里没有，会 ImportError。
# 这个错误信息只说 "No module named 'faiss'"，不看 libgomp 完全想不到是缺系统库。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖再拷代码：这样改代码不会让依赖层缓存失效，重建快很多
COPY web/requirements.txt /app/web/requirements.txt
RUN pip install --no-cache-dir -r /app/web/requirements.txt

COPY agent/ /app/agent/
COPY rag/   /app/rag/
COPY web/   /app/web/

# 中文日志不被 GBK 卡住；不加 -u 的话容器里的输出会被缓冲，
# docker logs 看不到实时进度，调试 SSE 时会以为程序卡死了
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app/web
EXPOSE 8000

# compose 里的 healthcheck 也有一份；这里写一份是为了直接 docker run 时也能用
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
