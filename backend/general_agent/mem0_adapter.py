"""Mem0 1.0.11 的局部 PostgreSQL 兼容层，不调用 SDK 默认 SQLite 生命周期。"""

import logging
import os
from importlib.metadata import version
from typing import Any
from uuid import UUID, uuid4

# 必须在首次导入 SDK 前禁用遥测；临时目录由 MemoryService 在懒加载前配置。
os.environ['MEM0_TELEMETRY'] = 'False'

from mem0 import Memory
from mem0.configs.base import MemoryConfig
from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.embeddings.base import EmbeddingBase
from mem0.vector_stores.pgvector import PGVector, OutputData
from psycopg_pool import ConnectionPool

from general_agent.calls import BoundCalls
from general_agent.database import Database
from general_agent.settings import StoredEmbeddingSettings

# SDK 会在日志中记录事实内容；本兼容层向外提供脱敏状态，不输出 SDK 原文日志。
logging.getLogger('mem0').disabled = True
logging.getLogger('mem0.memory.main').disabled = True
logging.getLogger('mem0.vector_stores.pgvector').disabled = True

COLLECTION = 'personal_memories'
PERSON = 'local-person'


class PgHistory:
    """实现所选 SDK 的全部历史契约；每次调用独立连接，可由 SDK 线程池使用。"""

    def __init__(self, database: Database) -> None:
        """绑定迁移管理的历史表，不在初始化时创建替代数据库。"""
        self.database = database
        self.closed = False

    def add_history(self, memory_id: str, old_memory: str | None, new_memory: str | None, event: str, *, created_at: str | None = None, updated_at: str | None = None, is_deleted: int = 0, actor_id: str | None = None, role: str | None = None) -> None:
        """按 SDK 历史字段提交一条记录，保持旧值、新值与事件时序。"""
        if self.closed:
            raise RuntimeError('记忆历史适配器已关闭。')
        with self.database.connect() as connection:
            connection.execute('INSERT INTO mem0_history(id,memory_id,old_memory,new_memory,event,created_at,updated_at,is_deleted,actor_id,role) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (str(uuid4()), memory_id, old_memory, new_memory, event, created_at, updated_at, bool(is_deleted), actor_id, role))

    def get_history(self, memory_id: str) -> list[dict[str, Any]]:
        """按持久化顺序返回 SDK 兼容历史，数据库内部序号不作为历史字段外露。"""
        with self.database.connect() as connection:
            rows = connection.execute('SELECT * FROM mem0_history WHERE memory_id=%s ORDER BY seq', (memory_id,)).fetchall()
        return [{key: value for key, value in row.items() if key != 'seq'} for row in rows]

    def purge(self, memory_id: str) -> None:
        """删除指定记忆的全部历史正文，供后续明确删除操作使用。"""
        with self.database.connect() as connection:
            connection.execute('DELETE FROM mem0_history WHERE memory_id=%s', (memory_id,))

    def reset(self) -> None:
        """仅清空本兼容层管理的历史，不重新构造 SQLite。"""
        with self.database.connect() as connection:
            connection.execute('DELETE FROM mem0_history')

    def close(self) -> None:
        """幂等终止历史写入；短连接已由各方法释放。"""
        self.closed = True


class MeteredEmbedding(EmbeddingBase):
    """用操作专属网关替换默认嵌入客户端，SDK 内部线程仍携带预算身份。"""

    def __init__(self, settings: StoredEmbeddingSettings, calls: BoundCalls | None, reserved: UUID | None = None) -> None:
        """保存显式配置及可选预留请求；只读会话不具有发出云请求的权限。"""
        super().__init__(BaseEmbedderConfig(model=settings.model, embedding_dims=settings.dimensions))
        self.settings, self.calls, self.reserved = settings, calls, reserved

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        """先计量实际嵌入，再检查取消；停止发生在响应期间时不继续落库。"""
        if self.calls is None:
            raise ValueError('只读记忆会话不能发出嵌入请求。')
        reserved, self.reserved = self.reserved, None
        result = self.calls.embed(self.settings, [text], reserved)
        with self.calls.gateway.budget.database.connect() as connection:
            row = connection.execute('SELECT cancelled FROM cloud_operations WHERE id=%s', (self.calls.context.operation_id,)).fetchone()
            if row is None or row['cancelled']:
                raise ValueError('保存已停止；尚未写入记忆。')
        return list(result.data[0].embedding)


class DisabledInference:
    """本产品只向 Mem0 提交已批准正文，拒绝其默认自动提取模型路径。"""

    def generate_response(self, *args: Any, **kwargs: Any) -> str:
        """若误用 infer=True 则立即拒绝，不实例化或调用默认云端模型。"""
        raise ValueError('请先由应用确认具体事实；禁止 Mem0 自动推断写入。')


class SimilarityPGVector(PGVector):
    """将该版本官方 PGVector 距离转换为 Mem0 约定的相似度。"""

    def search(self, query: str, vectors: list[float], limit: int = 5, filters: dict[str, Any] | None = None) -> list[OutputData]:
        """复用官方查询并将 cosine distance 转为越大越相关的分数。"""
        rows = super().search(query, vectors, limit, filters)
        for row in rows:
            if row.score is not None:
                row.score = 1.0 - row.score
        return list(rows)


class PgMemory(Memory):
    """锁定版本，仅接管生命周期；add/get/search/update/delete 复用真实 OSS SDK。"""

    def __init__(self, database: Database, settings: StoredEmbeddingSettings, calls: BoundCalls | None = None, reserved: UUID | None = None) -> None:
        """显式注入 PostgreSQL 历史和计量适配，失败时也关闭自建连接池。"""
        if version('mem0ai') != '1.0.11':
            raise RuntimeError('Mem0 版本不符，必须先重新验证 PostgreSQL 兼容层。')
        self.config = MemoryConfig(vector_store={'provider': 'pgvector', 'config': {'connection_string': database.url, 'collection_name': COLLECTION, 'embedding_model_dims': settings.dimensions, 'hnsw': False}}, llm={'provider': 'openai', 'config': {'enable_vision': False}})
        self.custom_fact_extraction_prompt = None
        self.custom_update_memory_prompt = None
        self.embedding_model = MeteredEmbedding(settings, calls, reserved)
        self.llm = DisabledInference()
        self.db = PgHistory(database)
        self.collection_name = COLLECTION
        self.api_version = self.config.version
        self.reranker = None
        self.enable_graph = False
        self.graph = None
        self.pool = ConnectionPool(database.url, min_size=1, max_size=2, timeout=5, kwargs={'connect_timeout': 5}, open=True)
        try:
            # 固定集合名和已验证整数维度不允许外部 SQL 标识符注入。
            self.vector_store = SimilarityPGVector(dbname=None, collection_name=COLLECTION, embedding_model_dims=settings.dimensions, user=None, password=None, host=None, port=None, diskann=False, hnsw=False, connection_pool=self.pool)
        except BaseException:
            self.close()
            raise

    def update(self, memory_id: str, data: str, metadata: dict[str, Any] | None = None) -> Any:
        """修复 1.0.11 更新会丢失来源的行为；身份键始终沿用已有正式条目。"""
        existing = self.get(memory_id)
        if existing is None:
            raise ValueError('记忆条目不存在。')
        merged = {**(existing.get('metadata') or {}), **(metadata or {})}
        for key in ('user_id','agent_id','run_id','actor_id','role'):
            merged.pop(key,None)
            if existing.get(key) is not None:
                merged[key] = existing[key]
        return super().update(memory_id,data,metadata=merged)

    def reset(self) -> None:
        """重置向量与内部历史；应用业务动作必须由上层维护流程另行协调。"""
        self.vector_store.reset()
        self.db.reset()

    def close(self) -> None:
        """幂等释放历史状态和向量连接池，不依赖 SDK 析构器回收。"""
        self.db.close()
        self.pool.close()
