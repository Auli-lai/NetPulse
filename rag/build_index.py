"""
建索引 —— 把日志和知识库灌进向量库。

    改完 chunking 逻辑，或者换了 embedding 模型，都要重跑这个脚本。

用法：

    # 用自带的样本日志（不用起 eBPF 服务，不用 root）
    python build_index.py --sample

    # 用真实日志
    python build_index.py --logs /var/log/netpulse/server.log

    # 不加 --logs 时，会把 --sample 和 --logs 的日志合并

    # 离线模式：不调 embedding API，验证链路是否通（检索质量不作数）
    python build_index.py --sample --offline

注意：换了 embedding 模型或维度，必须加 --rebuild 删掉旧索引。
     不然会用 1024 维的旧库去装 512 维的新向量，FAISS 直接报维度错误 ——
     这算运气好的，更坑的是维度恰好一样但模型换了，不报错、召回率悄悄下降。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from embedder import build_embedder
from chunking import build_chunks
from knowledge import load_knowledge
from vector_store import VectorStore

import config

SAMPLE_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "sample_logs.txt")


def collect_log_text(paths: list[str]) -> str:
    parts: list[str] = []
    for path in paths:
        if not os.path.exists(path):
            print(f"⚠️  日志文件不存在，跳过：{path}")
            continue
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        parts.append(content)
        print(f"  读入 {path}（{len(content.splitlines())} 行）")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 RAG 检索索引")
    parser.add_argument("--logs", nargs="*", default=[], help="真实日志文件路径，可多个")
    parser.add_argument("--sample", action="store_true", help="使用自带样本日志")
    parser.add_argument("--offline", action="store_true", help="离线模式，不调 embedding API")
    parser.add_argument("--no-knowledge", action="store_true", help="不索引知识库")
    parser.add_argument("--rebuild", action="store_true", help="删掉旧索引重建")
    args = parser.parse_args()

    log_paths = list(args.logs)
    if args.sample or not log_paths:
        log_paths.append(SAMPLE_LOG)

    if args.rebuild and os.path.isdir(config.INDEX_DIR):
        shutil.rmtree(config.INDEX_DIR)
        print(f"已删除旧索引 {config.INDEX_DIR}")

    # ---- 1. 收集语料 ----
    print("=" * 66)
    print("步骤 1/4  收集语料")
    print("=" * 66)
    log_text = collect_log_text(log_paths)

    knowledge = {} if args.no_knowledge else load_knowledge()
    if knowledge:
        print(f"  读入知识库（{len(knowledge)} 个大类）")

    if not log_text and not knowledge:
        print("❌ 没有任何语料，退出")
        return 1

    # ---- 2. 切分 ----
    print()
    print("=" * 66)
    print("步骤 2/4  结构感知切分")
    print("=" * 66)
    chunks = build_chunks(
        log_text=log_text or None,
        knowledge=knowledge or None,
        window_seconds=config.CHUNK_WINDOW_SECONDS,
    )
    n_log = sum(1 for c in chunks if c.metadata.get("source") == "log")
    n_kb = len(chunks) - n_log
    print(f"  日志 chunk {n_log} 个（时间窗 {config.CHUNK_WINDOW_SECONDS}s）")
    print(f"  知识 chunk {n_kb} 个（按知识条目切）")
    print(f"  合计 {len(chunks)} 个")

    if not chunks:
        print("❌ 一个 chunk 都没切出来，检查日志格式是不是和 chunking.py 的正则对得上")
        return 1

    # ---- 3. 向量化 ----
    print()
    print("=" * 66)
    print("步骤 3/4  向量化")
    print("=" * 66)
    embedder = build_embedder(offline=args.offline)
    print(f"  模型: {getattr(embedder, 'model', 'offline-hash')}  维度: {embedder.dim}")
    t0 = time.time()
    store = VectorStore(dim=embedder.dim)
    store.add(chunks, embedder)
    print(f"  完成，耗时 {time.time() - t0:.1f}s")

    # ---- 4. 落盘 ----
    print()
    print("=" * 66)
    print("步骤 4/4  保存索引")
    print("=" * 66)
    store.save(config.INDEX_DIR)
    print(f"  {config.INDEX_DIR}")
    print(f"    dense.faiss   {len(store)} 条向量")
    print(f"    chunks.json   chunk 原文与元数据")

    print()
    print("✅ 索引构建完成。下一步：")
    print("    python eval_recall.py       # 看混合检索比单路强多少")
    print("    python demo.py              # 问一个真实问题")
    return 0


if __name__ == "__main__":
    from console import enable_utf8_output

    enable_utf8_output()
    sys.exit(main())
