"""
知识库摄入服务 — 文档切分、向量化、upsert 到 ChromaDB

使用 upsert 而非 add，确保重复摄入不产生重复 chunk。
chunk ID = sha256(source_path:chunk_index:content_hash)
"""
import os
import hashlib
from typing import Optional

from .schema import EXTERNAL_KB_COLLECTIONS, REQUIRED_METADATA_FIELDS, EXTERNAL_KB_PREFIX
from .loaders import KnowledgeDocument, load_all_sources


class TextChunker:
    """文本切分器"""

    def __init__(self, chunk_size: int = 800, chunk_overlap: int = 120):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str) -> list[str]:
        """按 chunk_size 切分，带 overlap"""
        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size
            chunk = text[start:end]

            # 尝试在句号或换行处切分
            if end < len(text):
                for sep in ['\n\n', '\n', '。', '. ', '；', '; ']:
                    last_sep = chunk.rfind(sep)
                    if last_sep > self.chunk_size * 0.5:
                        end = start + last_sep + len(sep)
                        chunk = text[start:end]
                        break

            chunks.append(chunk.strip())
            start = end - self.chunk_overlap
            if start >= len(text):
                break

        return [c for c in chunks if len(c) > 20]


class KnowledgeIngestionService:
    """知识库摄入服务"""

    def __init__(self, chroma_manager, embedding_fn=None, config: dict = None):
        """
        Args:
            chroma_manager: ChromaDBManager 单例
            embedding_fn: embedding 函数，签名为 fn(text) -> list[float]
            config: 配置字典
        """
        self.chroma_manager = chroma_manager
        self.config = config or {}
        self.chunk_size = int(os.getenv("EXTERNAL_KB_CHUNK_SIZE", "800"))
        self.chunk_overlap = int(os.getenv("EXTERNAL_KB_CHUNK_OVERLAP", "120"))
        self.chunker = TextChunker(self.chunk_size, self.chunk_overlap)
        self._zhipu_failed = False  # 熔断器：429 后跳过智谱

        # embedding 函数 — 复用 FinancialSituationMemory 的 embedding 逻辑
        self._embedding_fn = embedding_fn

    def _get_embedding(self, text: str) -> list[float]:
        """获取文本的 embedding 向量（优先阿里百炼，其次智谱 GLM，最后降级零向量）"""
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

        # 返回零向量（不阻断流程）
        return [0.0] * 1024

    def _make_chunk_id(self, source_path: str, chunk_index: int, content_hash: str) -> str:
        """生成 chunk ID"""
        raw = f"{source_path}:{chunk_index}:{content_hash}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _ensure_metadata(self, doc: KnowledgeDocument, chunk_index: int,
                         content_hash: str) -> dict:
        """确保 metadata 包含所有必填字段"""
        meta = dict(doc.metadata)
        meta.setdefault("source_type", doc.source_type)
        meta.setdefault("source_path", doc.source_path)
        meta.setdefault("domain", doc.domain)
        meta.setdefault("version", "phase1")
        meta["chunk_index"] = chunk_index
        meta["content_hash"] = content_hash
        meta["created_by"] = "external_kb_ingestion"
        return meta

    def ingest_documents(self, collection_name: str,
                         docs: list[KnowledgeDocument]) -> int:
        """
        将文档列表摄入到指定 collection

        Returns:
            int: 摄入的 chunk 数量
        """
        if not docs:
            return 0

        collection = self.chroma_manager.get_or_create_collection(collection_name)

        all_ids = []
        all_documents = []
        all_embeddings = []
        all_metadatas = []

        for doc in docs:
            chunks = self.chunker.split(doc.text)
            for i, chunk_text in enumerate(chunks):
                content_hash = hashlib.sha256(
                    chunk_text.encode()
                ).hexdigest()[:16]

                chunk_id = self._make_chunk_id(doc.source_path, i, content_hash)
                embedding = self._get_embedding(chunk_text)
                metadata = self._ensure_metadata(doc, i, content_hash)

                all_ids.append(chunk_id)
                all_documents.append(chunk_text)
                all_embeddings.append(embedding)
                all_metadatas.append(metadata)

        if all_ids:
            # 如果 embedding 全为零向量，不传 embeddings，让 ChromaDB 用默认模型生成
            use_default_embedding = all(
                all(x == 0.0 for x in emb) for emb in all_embeddings
            )
            if use_default_embedding:
                collection.upsert(
                    ids=all_ids,
                    documents=all_documents,
                    metadatas=all_metadatas,
                )
            else:
                collection.upsert(
                    ids=all_ids,
                    documents=all_documents,
                    embeddings=all_embeddings,
                    metadatas=all_metadatas,
                )

        return len(all_ids)

    def ingest_from_directory(self, source_root: str,
                              reset: bool = False) -> dict:
        """
        从目录加载所有知识文档并摄入

        Args:
            source_root: 知识源目录
            reset: 是否先清空 collection（重新摄入）

        Returns:
            dict: {collection_name: chunk_count}
        """
        if not os.path.isdir(source_root):
            raise FileNotFoundError(f"知识源目录不存在: {source_root}")

        # 加载所有文档
        docs_by_collection = load_all_sources(source_root)

        # 可选：重置 collection
        if reset:
            for coll_name in docs_by_collection:
                try:
                    self.chroma_manager._client.delete_collection(coll_name)
                    # 清除缓存
                    if coll_name in self.chroma_manager._collections:
                        del self.chroma_manager._collections[coll_name]
                except Exception:
                    pass

        # 逐 collection 摄入
        result = {}
        for coll_name, docs in docs_by_collection.items():
            if not docs:
                continue
            count = self.ingest_documents(coll_name, docs)
            result[coll_name] = count

        return result

    def get_stats(self) -> dict:
        """获取各 collection 的统计信息"""
        stats = {}
        for coll_name in EXTERNAL_KB_COLLECTIONS:
            try:
                coll = self.chroma_manager.get_or_create_collection(coll_name)
                stats[coll_name] = {
                    "count": coll.count(),
                    "description": EXTERNAL_KB_COLLECTIONS[coll_name]["description"],
                }
            except Exception as e:
                stats[coll_name] = {"count": 0, "error": str(e)}
        return stats
