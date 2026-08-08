#!/usr/bin/env python3
"""
外部知识库摄入入口脚本

用法:
  # 摄入默认知识目录
  python scripts/ingest_external_kb.py

  # 指定知识源目录
  python scripts/ingest_external_kb.py --source ./knowledge_sources

  # 重新摄入（先清空再写入）
  python scripts/ingest_external_kb.py --reset

  # 仅查看统计
  python scripts/ingest_external_kb.py --stats-only

环境变量:
  EXTERNAL_KB_ENABLED        - 是否启用外部知识库 (默认 true)
  EXTERNAL_KB_SOURCE_DIR     - 默认知识源目录 (默认 ./knowledge_sources)
  EXTERNAL_KB_CHUNK_SIZE      - chunk 大小 (默认 800)
  EXTERNAL_KB_CHUNK_OVERLAP   - chunk 重叠 (默认 120)
  CHROMADB_PERSIST_DIR        - ChromaDB 持久化目录 (默认 ./data/chromadb)
  CHROMADB_IS_PERSISTENT     - ChromaDB 是否持久化 (默认 true)
  DASHSCOPE_API_KEY           - 阿里百炼 API Key (用于 embedding)
"""
import argparse
import os
import sys

# 确保项目根目录在 sys.path 中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from tradingagents.knowledge import KnowledgeIngestionService
from tradingagents.knowledge.schema import EXTERNAL_KB_COLLECTIONS
from tradingagents.agents.utils.memory import ChromaDBManager

from tradingagents.utils.logging_init import get_logger
logger = get_logger("default")


def main():
    parser = argparse.ArgumentParser(
        description="外部知识库摄入工具 — 将知识文档切分、向量化、写入 ChromaDB"
    )
    parser.add_argument(
        "--source", "-s",
        default=os.getenv("EXTERNAL_KB_SOURCE_DIR", "./knowledge_sources"),
        help="知识源目录路径 (默认: ./knowledge_sources 或 EXTERNAL_KB_SOURCE_DIR)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="重新摄入：先清空 collection 再写入",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="仅查看各 collection 统计信息，不执行摄入",
    )
    args = parser.parse_args()

    # 检查环境变量
    kb_enabled = os.getenv("EXTERNAL_KB_ENABLED", "true").lower() == "true"
    if not kb_enabled:
        logger.warning("EXTERNAL_KB_ENABLED=false, 外部知识库已禁用，退出。")
        print("[SKIP] EXTERNAL_KB_ENABLED=false, 跳过摄入。")
        return

    # 初始化 ChromaDB
    logger.info("初始化 ChromaDBManager ...")
    try:
        chroma_manager = ChromaDBManager()
    except Exception as e:
        logger.error(f"ChromaDBManager 初始化失败: {e}")
        print(f"[ERROR] ChromaDB 初始化失败: {e}")
        sys.exit(1)

    # 初始化摄入服务
    ingestion = KnowledgeIngestionService(
        chroma_manager=chroma_manager,
        config={},
    )

    # 仅统计模式
    if args.stats_only:
        print("\n" + "=" * 60)
        print("  外部知识库 Collection 统计")
        print("=" * 60)
        stats = ingestion.get_stats()
        total = 0
        for coll_name, info in stats.items():
            count = info.get("count", 0)
            total += count
            desc = info.get("description", "")
            status = "OK" if "error" not in info else f"ERROR: {info['error']}"
            print(f"  {coll_name:25s}  {count:6d} chunks  [{status}]")
            if desc:
                print(f"    {'':25s}  {desc}")
        print("=" * 60)
        print(f"  总计: {total} chunks")
        print("=" * 60 + "\n")
        return

    # 检查源目录
    source_dir = os.path.abspath(args.source)
    if not os.path.isdir(source_dir):
        logger.error(f"知识源目录不存在: {source_dir}")
        print(f"[ERROR] 知识源目录不存在: {source_dir}")
        print(f"\n请创建知识源目录并放入文档，目录结构示例:")
        print(f"  {source_dir}/")
        print(f"    kb_chanlun_rules/    — 缠论规则文档")
        print(f"    kb_trade_playbook/   — 交易手册")
        print(f"    kb_case_review/      — 历史案例")
        print(f"    kb_workflow_docs/    — 工作流文档")
        print(f"\n或通过 --source 指定其他目录。")
        sys.exit(1)

    # 执行摄入
    logger.info(f"开始摄入知识源目录: {source_dir}")
    logger.info(f"  reset={args.reset}")
    logger.info(f"  chunk_size={ingestion.chunk_size}, chunk_overlap={ingestion.chunk_overlap}")

    print(f"\n[INFO] 源目录: {source_dir}")
    print(f"[INFO] reset: {args.reset}")
    print(f"[INFO] chunk_size: {ingestion.chunk_size}, overlap: {ingestion.chunk_overlap}")
    print()

    try:
        result = ingestion.ingest_from_directory(
            source_root=source_dir,
            reset=args.reset,
        )
    except Exception as e:
        logger.error(f"摄入失败: {e}", exc_info=True)
        print(f"\n[ERROR] 摄入失败: {e}")
        sys.exit(1)

    # 输出结果
    print("=" * 60)
    print("  摄入完成")
    print("=" * 60)
    total_chunks = 0
    for coll_name, count in result.items():
        print(f"  {coll_name:25s}  {count:6d} chunks")
        total_chunks += count
    print("-" * 60)
    print(f"  总计: {total_chunks} chunks")
    print("=" * 60)

    # 输出最终统计
    print("\n最终 Collection 统计:")
    stats = ingestion.get_stats()
    for coll_name, info in stats.items():
        count = info.get("count", 0)
        print(f"  {coll_name:25s}  {count:6d} chunks")

    print("\n[OK] 摄入完成。")


if __name__ == "__main__":
    main()
