"""
知识库只读检索接口 — KnowledgeRetriever

给 agent 使用的只读检索接口，不暴露 add/upsert/delete/reset。
根据 agent_role 选择可访问的 collection，返回带来源标记的证据片段。

隔离规则:
  - 只能访问 kb_ 前缀的 collection
  - 禁止访问 agent memory collection (bull_memory 等)
"""
import os
from typing import Optional

from .schema import (
    EXTERNAL_KB_COLLECTIONS,
    AGENT_MEMORY_COLLECTIONS,
    get_agent_accessible_collections,
    is_agent_memory,
)


class KnowledgeRetriever:
    """知识库只读检索器"""

    def __init__(self, chroma_manager, config: dict = None,
                 embedding_fn=None):
        """
        Args:
            chroma_manager: ChromaDBManager 单例
            config: 配置字典
            embedding_fn: embedding 函数 fn(text) -> list[float]
        """
        self.chroma_manager = chroma_manager
        self.config = config or {}
        self.default_top_k = int(os.getenv("EXTERNAL_KB_TOP_K", "5"))
        self._embedding_fn = embedding_fn
        self._enabled = os.getenv("EXTERNAL_KB_ENABLED", "true").lower() == "true"
        self._zhipu_failed = False  # 熔断器：429 后跳过智谱

    def _get_embedding(self, text: str) -> list[float]:
        """获取查询文本的 embedding（优先阿里百炼，其次智谱 GLM，最后降级零向量）"""
        if self._embedding_fn:
            return self._embedding_fn(text)

        # 方案 1: 阿里百炼 text-embedding-v3
        try:
            import dashscope
            from dashscope import TextEmbedding
            dashscope.api_key = os.getenv('DASHSCOPE_API_KEY', '')
            if dashscope.api_key:
                resp = TextEmbedding.call(
                    model="text-embedding-v3",
                    input=text[:50000],
                )
                if resp and resp.status_code == 200:
                    return resp.output['embeddings'][0]['embedding']
        except Exception:
            pass

        # 方案 2: 智谱 GLM embedding-2 (OpenAI 兼容模式)
        if not self._zhipu_failed:
            try:
                zhipu_key = os.getenv('ZHIPU_API_KEY', '')
                if zhipu_key:
                    from openai import OpenAI
                    client = OpenAI(
                        api_key=zhipu_key,
                        base_url='https://open.bigmodel.cn/api/paas/v4/',
                        max_retries=0,
                    )
                    resp = client.embeddings.create(
                        model='embedding-2',
                        input=text[:50000],
                    )
                    if resp and resp.data:
                        return resp.data[0].embedding
            except Exception:
                self._zhipu_failed = True  # 熔断：后续不再尝试

        return [0.0] * 1024

    def _validate_collection(self, collection_name: str):
        """校验 collection 是否可被检索（隔离检查）"""
        if is_agent_memory(collection_name):
            raise ValueError(
                f"KnowledgeRetriever cannot query agent memory collections: {collection_name}"
            )

    def retrieve(
        self,
        query: str,
        collections: list[str],
        top_k: int = None,
        where: dict = None,
    ) -> list[dict]:
        """
        检索多个 collection

        Args:
            query: 查询文本（市场情境/问题）
            collections: 要检索的 collection 名称列表
            top_k: 每个 collection 返回的最大结果数
            where: ChromaDB metadata 过滤条件

        Returns:
            list[dict]: 检索结果列表
        """
        if not self._enabled:
            return []

        top_k = top_k or self.default_top_k
        query_embedding = self._get_embedding(query)

        # 空向量检查：降级为文本搜索
        use_text_search = all(x == 0.0 for x in query_embedding)
        if use_text_search and not query.strip():
            return []

        results = []
        for coll_name in collections:
            self._validate_collection(coll_name)

            try:
                collection = self.chroma_manager.get_or_create_collection(coll_name)
                if collection.count() == 0:
                    continue

                actual_k = min(top_k, collection.count())

                if use_text_search:
                    # 降级：使用 ChromaDB 内置文本搜索
                    query_result = collection.query(
                        query_texts=[query],
                        n_results=actual_k,
                        where=where,
                    )
                else:
                    # 正常：使用 embedding 向量检索
                    query_result = collection.query(
                        query_embeddings=[query_embedding],
                        n_results=actual_k,
                        where=where,
                    )

                if not query_result or 'documents' not in query_result:
                    continue

                documents = query_result['documents'][0] if query_result['documents'] else []
                metadatas = query_result.get('metadatas', [[]])[0]
                distances = query_result.get('distances', [[]])[0]

                for i, doc in enumerate(documents):
                    meta = metadatas[i] if i < len(metadatas) else {}
                    distance = distances[i] if i < len(distances) else 1.0
                    results.append({
                        "collection": coll_name,
                        "text": doc,
                        "distance": distance,
                        "metadata": meta,
                    })

            except Exception:
                continue

        # 按距离排序（距离越小越相似）
        results.sort(key=lambda x: x.get("distance", 1.0))
        return results[:top_k * len(collections)]

    def retrieve_for_agent(
        self,
        query: str,
        agent_role: str,
        top_k: int = None,
    ) -> list[dict]:
        """
        根据 agent 角色检索可访问的外部知识

        Args:
            query: 查询文本
            agent_role: agent 角色 (bull_researcher / bear_researcher / trader / risk_manager / research_manager)
            top_k: 每个 collection 返回的最大结果数

        Returns:
            list[dict]: 检索结果列表
        """
        accessible = get_agent_accessible_collections(agent_role)
        if not accessible:
            return []

        return self.retrieve(query, accessible, top_k)

    def format_context(self, results: list[dict]) -> str:
        """
        将检索结果格式化为 prompt 可用的上下文文本

        Args:
            results: retrieve() 或 retrieve_for_agent() 的返回值

        Returns:
            str: 格式化的知识上下文文本
        """
        if not results:
            return ""

        lines = ["=== 外部知识库检索结果 ==="]
        for i, r in enumerate(results, 1):
            coll = r.get("collection", "unknown")
            text = r.get("text", "")
            meta = r.get("metadata", {})
            source = meta.get("source_path", "")
            domain = meta.get("domain", "")

            # 截断过长的文本
            if len(text) > 500:
                text = text[:500] + "..."

            lines.append(f"\n[{i}] 来源: {coll} ({domain})")
            if source:
                lines.append(f"    文件: {source}")
            lines.append(f"    内容: {text}")

        lines.append("\n=== 知识库检索结束 ===")
        return "\n".join(lines)
