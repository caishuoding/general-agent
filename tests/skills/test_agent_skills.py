"""通过真实聊天和模型 HTTP 接口验证 Skill 选择、指令注入与续接。"""

import json
from pathlib import Path
from typing import Any

import httpx

from tests.agent.test_multi_agent import response
from tests.auth.support import authenticated_client, set_global_settings


def test_model_selects_skill_and_resumes_task_without_automatic_activation(database_url: str, tmp_path: Path) -> None:
    """模型主动加载完整入口，下一条普通消息只看到任务摘要并可显式续接。"""
    task_id = ''
    loads = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """用外部模型协议驱动真实加载工具，检查唯一系统消息及完整指令。"""
        nonlocal task_id, loads
        payload: dict[str, Any] = json.loads(request.content)
        system = [message['content'] for message in payload['messages'] if message['role'] == 'system']
        assert len(system) == 1
        names = {tool['function']['name'] for tool in payload.get('tools', [])}
        assert 'load_skill' in names
        tools = [json.loads(message['content']) for message in payload['messages'] if message['role'] == 'tool']
        if not tools:
            assert 'AI-driven presentation workflow' in system[0]
            assert '# PPT Master Skill' not in system[0]
            if task_id:
                assert task_id in str(payload['messages'])
            loads += 1
            return response(name='load_skill', arguments={'name': 'ppt-master', 'task_id': task_id or None})
        task_id = tools[-1]['task_id']
        assert '# PPT Master Skill' in system[0]
        assert '/opt/skills/ppt-master' in system[0]
        return response('制作方案已准备，请确认配色。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        for content in ['根据报告制作演示文稿', '继续，用蓝色']:
            result = client.post(f'/api/conversations/{conversation}/messages', json={'content': content})
            assert result.status_code == 200, result.text
            assert result.json()['messages'][-1]['status'] == 'complete', result.text
        assert loads == 2
