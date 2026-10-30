"""通过聊天接口验证显式 Skill 描述拼接与用户原文快照。"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.agent.test_multi_agent import response
from tests.auth.support import authenticated_client, set_global_settings
from tests.conversation.test_streaming import wait_message


def test_selected_description_is_one_user_message_and_history_keeps_original(database_url: str, tmp_path: Path) -> None:
    """显式选择拼接完整描述，历史只保存原文并返回快照，后续不会重复展开。"""
    payloads: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """只记录实际模型请求，选择卡片不强制触发工具或创建任务。"""
        payloads.append(json.loads(request.content))
        return response('收到你的需求。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        description = client.get('/api/skills').json()[0]['description']
        conversation = client.post('/api/conversations', json={}).json()['id']
        sent = client.post(f'/api/conversations/{conversation}/messages', json={
            'content': '根据附件制作汇报', 'skills': ['ppt-master', 'ppt-master']})
        assert sent.status_code == 200, sent.text
        user = sent.json()['messages'][0]
        assert user['content'] == '根据附件制作汇报'
        assert len(user['skill_selections']) == 1
        assert user['skill_selections'][0]['description'] == description
        actual = [item for item in payloads[0]['messages'] if item['role'] == 'user']
        assert len(actual) == 1
        assert actual[0]['content'].count(description) == 1
        assert actual[0]['content'].endswith('用户需求：\n根据附件制作汇报')
        assert client.get(f'/api/conversations/{conversation}/skill-tasks').json() == []
        continued = client.post(f'/api/conversations/{conversation}/messages', json={'content': '继续讨论'})
        assert continued.status_code == 200
        historical = [item['content'] for item in payloads[-1]['messages'] if item['role'] == 'user']
        assert sum(item.count(description) for item in historical) == 1
        assert historical[-1] == '继续讨论'
        rejected = client.post(f'/api/conversations/{conversation}/runs', json={
            'content': '不要忽略失效选择', 'skills': ['missing-skill']})
        assert rejected.status_code == 422


def test_retry_uses_original_ordered_snapshots_after_catalog_upgrade(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """多选保持顺序；失败后升级目录再重启应用，流式重试仍使用原描述快照。"""
    catalog = tmp_path / 'catalog'
    for name in ('alpha', 'beta'):
        root = catalog / name
        root.mkdir(parents=True)
        (root / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: |\n  {name} 第一行\n  完整第二行\nmetadata:\n  version: "1"\n---\n# 入口\n')
    monkeypatch.setenv('SKILLS_DIR', str(catalog))
    failing = True
    observed: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """首次制造外部模型失败，重试时记录真实输入并给出最终回答。"""
        if failing:
            return httpx.Response(400, json={'error': {'message': 'controlled failure'}})
        observed.append(json.loads(request.content))
        return response('重试完成。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        failed = client.post(f'/api/conversations/{conversation}/messages', json={'content': '联合处理需求', 'skills': ['beta', 'alpha', 'beta']})
        assert failed.status_code == 502
        history = client.get(f'/api/conversations/{conversation}').json()['messages']
        assert [item['name'] for item in history[0]['skill_selections']] == ['beta', 'alpha']
        answer = history[-1]['id']
    for name in ('alpha', 'beta'):
        (catalog / name / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 新版描述不应进入旧消息\nmetadata:\n  version: "2"\n---\n# 入口\n')
    failing = False
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        assert client.get('/api/skills').json()[0]['version'] == '2'
        retried = client.post(f'/api/conversations/{conversation}/messages/{answer}/retry')
        assert retried.status_code == 202
        wait_message(client, conversation, 'complete', '重试完成。')
        actual = next(item['content'] for item in observed[0]['messages'] if item['role'] == 'user')
        assert actual.index('Skill：beta') < actual.index('Skill：alpha')
        assert actual.count('完整第二行') == 2
        assert actual.count('联合处理需求') == 1
        assert '新版描述' not in actual
