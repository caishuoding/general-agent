"""验证管理端全局 Token 汇总及访问权限。"""

from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from general_agent.app import create_app
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationService
from general_agent.infrastructure.provider import TokenUsage
from tests.auth.support import admin_client


def test_admin_usage_summary_counts_all_known_tokens(database_url: str, tmp_path: Path) -> None:
    """汇总多个账号的已知用量，排除搜索 credits 和未执行调用。"""
    with admin_client(database_url, tmp_path) as admin:
        users = [admin.post('/api/admin/users', json={'username': name, 'password': 'temporary-pass'}).json()['id'] for name in ('alice', 'bob')]
        call_ids = []
        with psycopg.connect(database_url) as connection:
            for user, unit, status, total in [
                (users[0], 'chat_tokens', 'complete', 12000),
                (users[1], 'chat_tokens', 'failed', 300),
                (users[1], 'chat_tokens', 'stopped', None),
                (users[0], 'credits', 'complete', None),
                (users[0], 'chat_tokens', 'cancelled', None),
                (users[0], 'chat_tokens', 'reserved', None),
            ]:
                call_id = uuid4()
                call_ids.append(call_id)
                connection.execute(
                    'INSERT INTO model_calls(id,user_id,model,unit,status,total_tokens,prompt_tokens,completion_tokens) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                    (call_id, user, 'test', unit, status, total, total - 100 if total is not None else None, 100 if total is not None else None),
                )
        operations = OperationService(Database(database_url))
        operations.record(call_ids[0], TokenUsage.from_payload({'prompt_tokens_details': {'cached_tokens': 6000}}))
        operations.record(call_ids[1], TokenUsage.from_payload({'prompt_tokens_details': {'cached_tokens': 0}}))
        # 后续只写入终态的记录不能覆盖已经接收的缓存数据。
        operations.record(call_ids[0], TokenUsage(), 'complete')
        assert admin.get('/api/admin/usage-summary').json() == {'total_tokens': 12300, 'unknown_calls': 1, 'prompt_tokens': 12100, 'completion_tokens': 200, 'unknown_prompt_calls': 1, 'unknown_completion_calls': 1, 'cached_tokens': 6000, 'cache_prompt_tokens': 12100, 'unknown_cache_calls': 1}
        with TestClient(create_app(database_url, tmp_path)) as anonymous:
            assert anonymous.get('/api/admin/usage-summary').status_code == 401


def test_cache_usage_rejects_invalid_values() -> None:
    """只接受响应中合法的嵌套缓存 Token，缺失和非法值保持未知。"""
    for value in (None, -1, True, '5', 2147483648):
        assert TokenUsage.from_payload({'prompt_tokens_details': {'cached_tokens': value}}).cached_tokens is None
    assert TokenUsage.from_payload({'cached_tokens': 4}).cached_tokens is None
    assert TokenUsage.from_payload({'prompt_tokens_details': {'cached_tokens': 0}}).cached_tokens == 0
