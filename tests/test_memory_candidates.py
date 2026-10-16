"""升级兼容：旧候选不会因启用原生自动记忆而被隐式批准。"""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
from fastapi.testclient import TestClient
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row

from general_agent.app import create_app
from test_memory import configure_memory
from test_memory_recall import recall_provider


def candidate_provider(request: httpx.Request) -> httpx.Response:
    """仅用于旧候选恢复接口的受控模型，新的自动流程不应调用此提示。"""
    payload = json.loads(request.content)
    if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
        body = {'candidates': [{'fact': '我喜欢简洁的中文回答', 'quote': '我喜欢简洁的中文回答', 'subject': 'user', 'target_id': None}]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(body, ensure_ascii=False)}}], 'usage': {'prompt_tokens': 11, 'completion_tokens': 8, 'total_tokens': 19}})
    return recall_provider(request)


def seed_legacy_candidate(database_url: str, conversation: str, message: str, fact: str, target: dict[str, Any] | None = None) -> dict[str, Any]:
    """在数据库边界重建升级前的待确认记录，不让新聊天流程伪装成旧行为。"""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        row = connection.execute("INSERT INTO memory_candidates(id,source_message_id,source_conversation_id,source_quote,fact,subject,status,target_id,target_version,target_fact) VALUES(%s,%s,%s,%s,%s,'user','pending',%s,%s,%s) RETURNING *", (uuid4(), message, conversation, fact, fact, target['id'] if target else None, target['version'] if target else None, target['fact'] if target else None)).fetchone()
    assert row is not None
    return dict(jsonable_encoder(row))


def test_legacy_candidates_remain_pending_after_upgrade_and_restart(database_url: str, tmp_path: Path) -> None:
    """旧候选保持未生效，新输入不触发旧候选模型；重启后仍可拒绝或显式保存旧条目。"""
    transport = httpx.MockTransport(candidate_provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '我喜欢简洁的中文回答'}).json()['messages'][-1]
        assert answer['memory_candidates'] == []
        candidate = seed_legacy_candidate(database_url, conversation, answer['reply_to'], '我喜欢简洁的中文回答')
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        assert client.get('/api/memories').json() == []
        card = client.get(f'/api/conversations/{conversation}').json()['messages'][-1]['memory_candidates'][0]
        assert card['status'] == 'pending' and card['approved_at'] is None
        assert client.post(f"/api/memory-candidates/{candidate['id']}/confirm").status_code == 200
        assert len(client.get('/api/memories').json()) == 1
