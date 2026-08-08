"""
外部知识库模块 — RAG Layer

提供外部交易知识的持久化存储、摄入和只读检索能力。
与现有 agent 记忆 (FinancialSituationMemory) 隔离，使用 kb_ 前缀区分。

阶段一：持久化 ChromaDB + 外部知识摄入 + agent 只读检索
"""
from .schema import (
    EXTERNAL_KB_COLLECTIONS,
    AGENT_MEMORY_COLLECTIONS,
    EXTERNAL_KB_PREFIX,
)
from .retriever import KnowledgeRetriever
from .ingestion import KnowledgeIngestionService

__all__ = [
    "EXTERNAL_KB_COLLECTIONS",
    "AGENT_MEMORY_COLLECTIONS",
    "EXTERNAL_KB_PREFIX",
    "KnowledgeRetriever",
    "KnowledgeIngestionService",
]
