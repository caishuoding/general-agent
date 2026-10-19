"""验证跨会话历史聊天检索已从公开聊天与数据库结构中移除。"""

import json
from pathlib import Path
from typing import Any

import httpx
import psycopg
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.attachments.test_attachments import configure
from tests.search.test_search import reply


def test_history_phrase_no_longer_reads_other_conversations(database_url: str, tmp_path: Path) -> None:
    """旧查询句式按普通聊天处理，响应不再包含历史字段，也不把旧消息发送给模型。"""
    payloads: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """记录模型边界并返回固定文本，供后续检查旧会话正文没有进入查询请求。"""
        payload = json.loads(request.content)
        payloads.append(payload)
        return reply(payload, '历史聊天检索已移除。')

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        original = configure(client)
        assert client.post(f'/api/conversations/{original}/messages', json={'content': '蓝鲸计划周五交付。'}).status_code == 200
        current = client.post('/api/conversations', json={'title': '普通会话'}).json()['id']
        answer = client.post(f'/api/conversations/{current}/messages', json={'content': '查找旧聊天：蓝鲸'}).json()['messages'][-1]
        assert answer['status'] == 'complete'
        assert answer['content'] == '历史聊天检索已移除。'
        assert 'history_search' not in answer and 'history_sources' not in answer
        assert payloads and '蓝鲸计划周五交付' not in str(payloads[-1]['messages'])


def test_history_tables_are_removed_by_migration(database_url: str, tmp_path: Path) -> None:
    """应用启动执行迁移后，历史查询与出处表不再存在。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """返回固定文本，仅供应用启动和迁移使用。"""
        return reply(json.loads(request.content), '未调用。')

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000'):
        with psycopg.connect(database_url) as connection:
            rows = connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('history_queries','history_hits')",
            ).fetchall()
        assert rows == []
