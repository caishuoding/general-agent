"""通过聊天执行验证已加载 Skill 随会话 State 续接。"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.agent.test_multi_agent import response
from tests.auth.support import authenticated_client, set_global_settings


def test_next_message_restores_skill_without_model_save_tool(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模型只读写工作文件，下一条消息自动恢复原任务、指令和工具权限。"""
    catalog = tmp_path / 'catalog' / 'demo'
    catalog.mkdir(parents=True)
    (catalog / 'SKILL.md').write_text('---\nname: demo\ndescription: 工作文件\n---\n# 测试指令\n按需读写文件。')
    monkeypatch.setenv('SKILLS_DIR', str(catalog.parent))
    task = ''
    phase = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """按真实工具返回保存任务身份，跨消息直接使用已恢复工具。"""
        nonlocal task, phase
        payload: dict[str, Any] = json.loads(request.content)
        names = [tool['function']['name'] for tool in payload['tools']]
        assert 'save_skill_task' not in names
        phase += 1
        if phase == 1:
            return response(name='load_skill', arguments={'name': 'demo'})
        if phase == 2:
            task = json.loads(payload['messages'][-1]['content'])['task_id']
            return response(name='write_work_file', arguments={'task_id': task, 'path': 'notes.txt', 'content': '原任务文件'})
        if phase == 3:
            return response('已写入，请继续。')
        if phase == 4:
            assert 'read_work_file' in names
            assert '# 测试指令' in payload['messages'][0]['content']
            return response(name='read_work_file', arguments={'task_id': task, 'path': 'notes.txt'})
        assert '原任务文件' in payload['messages'][-1]['content']
        return response('已读取原任务文件。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        first = client.post(f'/api/conversations/{conversation}/messages', json={'content': '写入文件'})
        assert first.status_code == 200, first.text
        tasks = client.get(f'/api/conversations/{conversation}/skill-tasks').json()
        assert len(tasks) == 1 and tasks[0]['status'] == 'waiting'
        next_answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取之前文件'})
        assert next_answer.status_code == 200, next_answer.text
        assert next_answer.json()['messages'][-1]['content'] == '已读取原任务文件。'
        assert len(client.get(f'/api/conversations/{conversation}/skill-tasks').json()) == 1
