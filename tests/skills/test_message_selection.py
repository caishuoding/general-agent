"""验证前端显式选择把完整 Skill 正文作为普通用户消息发送。"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.agent.test_multi_agent import response
from tests.auth.support import authenticated_client, set_global_settings


def _write_skill(root: Path, name: str, description: str, body: str) -> str:
    """创建最小测试 Skill 并返回完整 SKILL.md。"""
    skill = root / name
    skill.mkdir(parents=True)
    content = f'---\nname: {name}\ndescription: {description}\nmetadata:\n  version: "1"\n---\n{body}\n'
    (skill / 'SKILL.md').write_text(content, encoding='utf-8')
    return content


def test_explicit_full_skill_content_is_sent_once_with_names_only(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多选前端拼接完整正文，后端只保存名称且不再注入 description。"""
    catalog = tmp_path / 'catalog'
    alpha = _write_skill(catalog, 'alpha', '旧说明不应自动拼接', '# Alpha\n执行 A。')
    beta = _write_skill(catalog, 'beta', '另一条说明不应自动拼接', '# Beta\n执行 B。')
    monkeypatch.setenv('SKILLS_DIR', str(catalog))
    payloads: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """记录实际模型消息并返回普通文本，不强制加载任何工具。"""
        payloads.append(json.loads(request.content))
        return response('收到完整 Skill。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        content = f'【Skill：alpha】\n\n{alpha}\n\n【Skill：beta】\n\n{beta}\n\n【用户需求】\n联合处理'
        sent = client.post(f'/api/conversations/{conversation}/messages', json={
            'content': content, 'skills': ['alpha', 'beta', 'alpha']})
        assert sent.status_code == 200, sent.text
        user = sent.json()['messages'][0]
        assert user['content'] == content
        assert user['skill_names'] == ['alpha', 'beta']
        actual = [item for item in payloads[0]['messages'] if item['role'] == 'user']
        assert len(actual) == 1
        assert actual[0]['content'] == content
        assert 'Description：' not in actual[0]['content']
        missing_body = client.post(f'/api/conversations/{conversation}/messages', json={
            'content': '只有名称没有完整正文', 'skills': ['alpha']})
        assert missing_body.status_code == 422
        rejected = client.post(f'/api/conversations/{conversation}/runs', json={
            'content': '不要忽略失效选择', 'skills': ['missing-skill']})
        assert rejected.status_code == 422
