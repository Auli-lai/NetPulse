"""
RAG 配置 —— 所有外部依赖的坐标集中在这里，不散落到各个文件。

===========================================================================
本模块最容易让人困惑的一点：一个 API Key，要打三个不同协议的端点。
===========================================================================

    用途        协议                     端点
    ---------   ----------------------   ------------------------------------
    生成(LLM)   Anthropic Messages       /apps/anthropic
    向量化      OpenAI 兼容              /compatible-mode/v1
    重排        DashScope 原生           /api/v1/services/rerank/...

【为什么生成用 Anthropic 协议，向量化却要换成 OpenAI 协议？】

因为 Anthropic 的 API 协议里根本没有 embedding 接口 —— Anthropic 官方
就不提供向量化模型。百炼的 Anthropic 兼容端点只转发它支持的那部分能力，
所以向量化必须走另一条路（百炼的 text-embedding 挂在 OpenAI 兼容端点上）。

同理 Rerank 也不在 Anthropic 协议里，走 DashScope 自己的原生接口。

【为什么你的 base_url 结尾没有 /v1？】

百炼的 Anthropic 兼容端点只提供 /v1/messages，不提供 /v1/models。
base_url 要停在 /apps/anthropic，多写一个 /v1/ 就会拼成 /v1/v1/messages 而 404。
"""

from __future__ import annotations

import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# .env 支持
# ---------------------------------------------------------------------------
#
# ⚠️ 先澄清一个很常见的误解：
#
#     代码里写 os.environ.get("QWEN_API_KEY")
#     不等于"key 配好了"。
#
# 这一行的意思是「**去环境变量里读**」。它读不到就是读不到 ——
# 你必须在这个进程**启动之前**，让 QWEN_API_KEY 已经存在于环境里。
# 在源码里写下这行代码，一个字符的 key 都不会产生。
#
# 为什么要有 .env：因为"配环境变量"这件事在多个终端 + WSL/Windows 双环境下
# 太容易出错 —— 在 A 终端 export，在 B 终端起服务；或者在 PowerShell 里 export，
# 服务跑在 WSL 里（两个完全独立的环境）。这两种都会表现为"我明明配了"。
#
# 有了 .env，只要写在项目根目录一个文件里，所有入口（agent / web / mcp_server）
# 都会自动读到。**已存在的环境变量优先**，所以临时 export 仍然能覆盖它。
def _load_dotenv(path: str) -> None:
    """极简 .env 加载器（不引第三方依赖）。

    规则和大多数实现一致：
      · KEY=VALUE 一行一条，忽略空行和 # 开头的注释
      · 值两边的引号会去掉
      · **不覆盖已经存在的环境变量**（显式 export 的优先）
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        # 读不了就算了，不该因为一个配置文件让整个程序起不来
        pass


for _candidate in (
    os.path.join(_PROJECT_ROOT, ".env"),          # 项目根目录
    os.path.join(os.getcwd(), ".env"),            # 当前工作目录
):
    _load_dotenv(_candidate)


# ---------------------------------------------------------------------------
# API Key —— 只从环境变量（或 .env）读，绝不硬编码
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("QWEN_API_KEY")


def require_api_key() -> str:
    """取 API Key，没有就报错。

    为什么不在模块顶层直接 raise：
    因为导入 config 的模块里，有相当一部分（切分、BM25、离线向量化）
    根本不需要 key。顶层抛异常会让"没配 key 就跑不了切分自检"
    这种荒唐事发生 —— 而且离线模式的意义就是**不用 key 也能跑通链路**。

    所以：需要 key 的地方自己调这个函数，模块导入永远不炸。
    """
    if not API_KEY:
        raise RuntimeError(
            "没有找到环境变量 QWEN_API_KEY。\n"
            "运行前先导出（Linux/WSL）：\n"
            "    export QWEN_API_KEY='sk-你的百炼key'\n"
            "Windows PowerShell：\n"
            "    $env:QWEN_API_KEY='sk-你的百炼key'\n"
            "百炼控制台 -> API-KEY 管理 里可以创建。\n"
            "（只想验证链路能不能跑通、不想配 key，加 --offline 参数）"
        )
    return API_KEY


# ---------------------------------------------------------------------------
# 1. 生成：Anthropic Messages 协议
# ---------------------------------------------------------------------------

ANTHROPIC_BASE_URL = os.environ.get(
    "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/apps/anthropic"
)

# 注意：这是 Qwen 的模型名，不是 Claude 的。
# Anthropic 兼容端点只接受百炼这边支持的 Qwen 系列模型名。
# 可选的常见值：qwen3-max / qwen3.7-max / qwen3.7-plus / qwen3.8-max
# 具体哪些对你的账号开放，以百炼控制台「模型广场」为准。
CHAT_MODEL = os.environ.get("QWEN_CHAT_MODEL", "qwen3-max")

MAX_TOKENS = int(os.environ.get("QWEN_MAX_TOKENS", "2048"))


# ---------------------------------------------------------------------------
# 2. 向量化：OpenAI 兼容协议
# ---------------------------------------------------------------------------

EMBEDDING_BASE_URL = os.environ.get(
    "QWEN_EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
EMBEDDING_MODEL = os.environ.get("QWEN_EMBEDDING_MODEL", "text-embedding-v3")

# text-embedding-v3 默认 1024 维（也支持 512/768，需在建库时通过 dimensions 指定）。
# 建库和查询必须用同一个维度，改了就要重建索引。
EMBEDDING_DIM = int(os.environ.get("QWEN_EMBEDDING_DIM", "1024"))

# 百炼 embedding 单次请求最多 10 条文本（批量接口有限制），超了要分批。
EMBEDDING_BATCH = int(os.environ.get("QWEN_EMBEDDING_BATCH", "10"))


# ---------------------------------------------------------------------------
# 3. 重排：DashScope 原生协议
# ---------------------------------------------------------------------------

RERANK_BASE_URL = os.environ.get(
    "QWEN_RERANK_BASE_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
)

# 旧的 gte-rerank-v2 已于 2026-05-30 下线，现在用 qwen3-rerank。
# 注意它的请求体是嵌套的 {"input": {...}, "parameters": {...}}，
# 和向量化那个 OpenAI 风格的扁平请求体完全不同 —— 见 rerank.py。
RERANK_MODEL = os.environ.get("QWEN_RERANK_MODEL", "qwen3-rerank")


# ---------------------------------------------------------------------------
# 检索参数
# ---------------------------------------------------------------------------

# 双路召回各取多少条候选，融合后再交给 rerank
DENSE_TOP_K = int(os.environ.get("RAG_DENSE_TOP_K", "20"))
SPARSE_TOP_K = int(os.environ.get("RAG_SPARSE_TOP_K", "20"))

# RRF 融合常数。见 hybrid.py 里对 k 的说明。
RRF_K = int(os.environ.get("RAG_RRF_K", "60"))

# 融合后送进 rerank 的候选数，以及 rerank 之后最终留给 LLM 的条数
RERANK_CANDIDATES = int(os.environ.get("RAG_RERANK_CANDIDATES", "20"))
FINAL_TOP_K = int(os.environ.get("RAG_FINAL_TOP_K", "5"))

# 日志切分：一个时间窗多长（秒）。见 chunking.py 对切分策略的说明。
CHUNK_WINDOW_SECONDS = int(os.environ.get("RAG_CHUNK_WINDOW", "30"))


# ---------------------------------------------------------------------------
# 索引落盘位置
# ---------------------------------------------------------------------------

INDEX_DIR = os.environ.get(
    "RAG_INDEX_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_store"),
)

# 离线冒烟模式：不调 embedding API，用确定性哈希假装向量。
# 仅用于在没网 / 不想花额度时验证"索引→召回→融合→重排"这条链路通不通，
# 它没有语义能力，绝对不能用来评测检索质量，更不能写进简历。
OFFLINE_EMBEDDING = os.environ.get("RAG_OFFLINE", "").lower() in ("1", "true", "yes")


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()

    import tempfile

    print("── 当前配置 ──")
    print(f"  CHAT_MODEL          {CHAT_MODEL}")
    print(f"  ANTHROPIC_BASE_URL  {ANTHROPIC_BASE_URL}")
    print(f"  EMBEDDING_MODEL     {EMBEDDING_MODEL}")
    print(f"  RERANK_MODEL        {RERANK_MODEL}")
    print(f"  INDEX_DIR           {INDEX_DIR}")
    print(f"  OFFLINE_EMBEDDING   {OFFLINE_EMBEDDING}")
    if API_KEY:
        # 只露后 4 位。够确认"是我想要的那个 key"，又不会把密钥打得到处都是
        # （终端记录、CI 日志、别人看你屏幕…）
        print(f"  API_KEY             …{API_KEY[-4:]}  （已读到，长度 {len(API_KEY)}）")
    else:
        print("  API_KEY             (未读到)")
        print()
        print("  ⚠️ os.environ.get(\"QWEN_API_KEY\") 是**去环境变量里读**，不是配好了。")
        print("     它必须在启动进程之前就在环境里。两种做法：")
        print("       · 写进项目根目录的 .env（cp .env.example .env）")
        print("       · 在启动服务的那个终端里 export QWEN_API_KEY='sk-...'")

    print()
    print("── .env 加载器自检 ──")
    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(
                "# 注释行\n"
                "\n"
                "NETPULSE_TEST_A=hello\n"
                "NETPULSE_TEST_B=\"带引号的值\"\n"
                "NETPULSE_TEST_C='单引号'\n"
                "坏行没有等号\n"
            )
        for k in ("NETPULSE_TEST_A", "NETPULSE_TEST_B", "NETPULSE_TEST_C"):
            os.environ.pop(k, None)

        _load_dotenv(env_path)

        cases = [
            ("普通值", os.environ.get("NETPULSE_TEST_A") == "hello"),
            ("双引号被去掉", os.environ.get("NETPULSE_TEST_B") == "带引号的值"),
            ("单引号被去掉", os.environ.get("NETPULSE_TEST_C") == "单引号"),
        ]
        failed = 0
        for name, ok in cases:
            print(f"  [{'OK  ' if ok else 'FAIL'}] {name}")
            failed += 0 if ok else 1

        # 已存在的环境变量不能被 .env 覆盖 —— 显式 export 的优先级更高
        os.environ["NETPULSE_TEST_A"] = "from-shell"
        _load_dotenv(env_path)
        ok = os.environ.get("NETPULSE_TEST_A") == "from-shell"
        print(f"  [{'OK  ' if ok else 'FAIL'}] 不覆盖已存在的环境变量（export 优先）")
        failed += 0 if ok else 1

        for k in ("NETPULSE_TEST_A", "NETPULSE_TEST_B", "NETPULSE_TEST_C"):
            os.environ.pop(k, None)

        if failed:
            print(f"\n  {failed} 项失败")
            raise SystemExit(1)
        print("\n  .env 加载器正常")
