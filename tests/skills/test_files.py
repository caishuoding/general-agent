"""验证生成文件只按用户、会话和当前 Run 授权下载。"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.agent.test_multi_agent import response
from tests.auth.support import authenticated_client, ensure_normal_user, login_user, set_global_settings


def test_generated_file_is_immutable_and_account_scoped(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 Skill 调用可写文件并发布；其他账号不能枚举或下载结果。"""
    catalog = tmp_path / 'catalog' / 'demo'
    catalog.mkdir(parents=True)
    content = '---\nname: demo\ndescription: 文件技能\nmetadata:\n  version: "1"\n---\n# Demo\n生成文件。'
    (catalog / 'SKILL.md').write_text(content, encoding='utf-8')
    monkeypatch.setenv('SKILLS_DIR', str(catalog.parent))
    phase = 0
    download_url = ''

    def provider(request: httpx.Request) -> httpx.Response:
        """按真实文件与发布结果推进，不伪造下载地址。"""
        nonlocal phase, download_url
        payload: dict[str, Any] = json.loads(request.content)
        tools = [json.loads(item['content']) for item in payload['messages'] if item['role'] == 'tool']
        if phase == 0:
            phase = 1
            return response(name='write_work_file', arguments={'path': 'report.txt', 'content': '原交付内容'})
        if phase == 1:
            assert tools[-1]['path'] == 'report.txt'
            phase = 2
            return response(name='publish_skill_file', arguments={'path': 'report.txt'})
        download_url = tools[-1]['url']
        phase = 3
        return response(f'下载：[文件]({download_url})')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        account = client.get('/api/auth/me').json()['id']
        sent = client.post(f'/api/conversations/{conversation}/messages', json={
            'content': f'【Skill：demo】\n\n{content}\n\n【用户需求】\n生成文件', 'skills': ['demo']})
        assert sent.status_code == 200, sent.text
        assert client.get(download_url).text == '原交付内容'
        files = client.get(f'/api/conversations/{conversation}/generated-files').json()
        assert files[0]['filename'] == 'report.txt'
        assert (tmp_path / 'generated' / account / conversation / 'workspace' / 'report.txt').is_file()
        assert (tmp_path / 'generated' / account / conversation / 'artifacts' / files[0]['id']).is_file()
        ensure_normal_user(database_url, 'another_user')
        login_user(client, 'another_user')
        assert client.get(download_url).status_code == 404
        assert client.get(f'/api/conversations/{conversation}/generated-files').status_code == 404
