"""封装附件向量缓存、余弦召回、RRF 融合及智谱重排的外部协议。"""

import json
import math
import os
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from general_agent.infrastructure.database import Database
from general_agent.infrastructure.provider import ModelClient
from general_agent.infrastructure.settings import EmbeddingSettingsStore

RERANK_URL = 'https://open.bigmodel.cn/api/paas/v4/rerank'


def chunk_key(row: dict[str, Any]) -> tuple[str, int]:
    """以附件身份和切片序号唯一识别候选，避免跨文档序号碰撞。"""
    return str(row['document_id']), int(row['chunk_id'])


def reciprocal_rank_fusion(rankings: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """以等权 RRF、k=60 融合各路名次，按切片身份去重并稳定处理同分。"""
    scores: dict[tuple[str, int], float] = defaultdict(float)
    rows: dict[tuple[str, int], dict[str, Any]] = {}
    for ranking in rankings:
        seen: set[tuple[str, int]] = set()
        for rank, row in enumerate(ranking, 1):
            key = chunk_key(row)
            if key in seen:
                continue
            seen.add(key)
            rows.setdefault(key, row)
            scores[key] += 1 / (60 + rank)
    ordered = sorted(rows, key=scores.__getitem__, reverse=True)
    return [rows[key] for key in ordered[:limit]]


class DocumentRetrieval:
    """复用既有嵌入配置并缓存附件向量，隔离智谱凭据和网络失败。"""

    def __init__(self, database: Database, directory: Path, transport: httpx.BaseTransport | None = None) -> None:
        """绑定私有配置和可替换 HTTP 传输，不在构造时进行网络访问。"""
        self.database, self.directory, self.transport = database, directory, transport
        self.embedding = EmbeddingSettingsStore(directory)
        self.model = ModelClient(transport)

    def semantic(self, rows: list[dict[str, Any]], query: str, limit: int,
                 refresh: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """逐批重新核验来源并补齐当前向量缓存，精确余弦排序；失败由工具降级。"""
        settings = self.embedding.read()
        if settings is None:
            raise ValueError('embedding_unavailable')
        query_result = self.model.embed(settings, [query])
        query_vector = query_result.data[0].embedding
        query_norm = math.hypot(*query_vector)
        if not query_norm:
            raise ValueError('embedding_unavailable')
        missing = [row for row in rows if row['embedding_revision'] != settings.revision or row['embedding'] is None]
        for start in range(0, len(missing), 16):
            batch = refresh(missing[start:start + 16])
            if not batch:
                continue
            result = self.model.embed(settings, [row['content'] for row in batch])
            updates: list[tuple[Any, ...]] = []
            for item in result.data:
                row = batch[item.index]
                if not math.hypot(*item.embedding):
                    raise ValueError('embedding_unavailable')
                updates.append((Jsonb(item.embedding), settings.revision, row['document_id'], row['chunk_id'], row['content']))
            # 仅更新仍存在且正文相同的授权切片，绝不通过插入重建删除来源。
            with self.database.connect() as connection, connection.cursor() as cursor:
                cursor.executemany(
                    'UPDATE document_chunks d SET embedding=%s,embedding_revision=%s FROM attachments a '
                    "WHERE d.attachment_id=%s AND d.ordinal=%s AND d.content=%s AND a.id=d.attachment_id "
                    "AND a.status='ready' AND a.expires_at>CURRENT_TIMESTAMP AND a.knowledge_version=2",
                    updates,
                )
        # 配置切换期间的旧空间不能继续用于这次查询。
        current = self.embedding.read()
        if current is None or current.revision != settings.revision:
            raise ValueError('embedding_unavailable')
        rows = refresh(rows)
        scored: list[tuple[float, int]] = []
        for index, row in enumerate(rows):
            vector = row['embedding']
            if row['embedding_revision'] != settings.revision or not isinstance(vector, list) or len(vector) != settings.dimensions:
                raise ValueError('embedding_unavailable')
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
                raise ValueError('embedding_unavailable')
            norm = math.hypot(*vector)
            if not norm:
                raise ValueError('embedding_unavailable')
            similarity = sum((left / query_norm) * (right / norm) for left, right in zip(query_vector, vector))
            scored.append((similarity, index))
        scored.sort(key=cosine_sort_key)
        return [rows[index] for _, index in scored[:limit]]

    def rerank(self, rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        """按官方 rerank 协议重排完整候选，严格验证结果身份，失败不回显响应或凭据。"""
        key = os.environ.get('ZHIPU_RERANK_API_KEY', '').strip()
        if not key:
            path = self.directory / 'rerank.json'
            if path.exists():
                saved = json.loads(path.read_text())
                if isinstance(saved, dict) and isinstance(saved.get('api_key'), str):
                    key = saved['api_key'].strip()
        if not key:
            raise ValueError('rerank_unavailable')
        if any(len(row['content']) > 4096 for row in rows):
            raise ValueError('rerank_input_too_long')
        with httpx.Client(transport=self.transport, timeout=10, follow_redirects=False, trust_env=False) as client:
            response = client.post(RERANK_URL, headers={'Authorization': 'Bearer ' + key}, json={
                'model': 'rerank', 'query': query, 'documents': [row['content'] for row in rows],
                'top_n': len(rows), 'return_documents': False,
            })
        response.raise_for_status()
        payload = response.json()
        results = payload.get('results') if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != len(rows):
            raise ValueError('rerank_unavailable')
        indices: set[int] = set()
        ordered: list[tuple[float, int]] = []
        for item in results:
            if not isinstance(item, dict):
                raise ValueError('rerank_unavailable')
            index, score = item.get('index'), item.get('relevance_score')
            if type(index) is not int or not 0 <= index < len(rows) or index in indices:
                raise ValueError('rerank_unavailable')
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
                raise ValueError('rerank_unavailable')
            indices.add(index)
            ordered.append((float(score), index))
        ordered.sort(key=cosine_sort_key)
        return [rows[index] for _, index in ordered]


def cosine_sort_key(item: tuple[float, int]) -> tuple[float, int]:
    """按相关性降序返回候选，同分时沿用输入次序。"""
    return -item[0], item[1]
