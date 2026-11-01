"""验证无状态 Skill 加载、相对路径工具和会话级多 Skill 名称。"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.agent.test_multi_agent import response
from tests.auth.support import authenticated_client, set_global_settings


def _catalog(tmp_path: Path) -> Path:
    """创建两个可区分的测试 Skill 目录。"""
    root = tmp_path / 'catalog'
    for name, body in (('demo', '# Demo\n执行演示。'), ('other', '# Other\n执行其他任务。')):
        skill = root / name
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: {name} 技能\nmetadata:\n  version: "1"\n---\n{body}\n',
            encoding='utf-8',
        )
    return root


def _tool_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """读取模型协议中真实返回的工具结果。"""
    return [json.loads(item['content']) for item in payload['messages'] if item['role'] == 'tool']


def test_autonomous_load_returns_content_and_relative_file_tools_need_no_task(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自主加载返回正文；第二个 Run 仍能读取同一会话上一 Run 写入的文件。"""
    monkeypatch.setenv('SKILLS_DIR', str(_catalog(tmp_path)))
    phase = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """按真实工具结果推进模型，验证工具集合和结果内容。"""
        nonlocal phase
        payload: dict[str, Any] = json.loads(request.content)
        messages = _tool_messages(payload)
        names = [tool['function']['name'] for tool in payload.get('tools', [])]
        assert 'load_skill' in names
        if phase == 0:
            assert 'write_work_file' not in names
            phase = 1
            return response(name='load_skill', arguments={'name': 'demo'})
        if phase == 1:
            loaded = messages[-1]
            assert loaded['name'] == 'demo'
            assert loaded['content'].startswith('---\nname: demo')
            assert 'task_id' not in loaded
            assert 'write_work_file' in names
            phase = 2
            return response(name='write_work_file', arguments={'path': 'notes.txt', 'content': '持久工作内容'})
        if phase == 2:
            assert messages[-1]['path'] == 'notes.txt'
            phase = 3
            return response(name='read_work_file', arguments={'path': 'notes.txt'})
        if phase == 3:
            assert messages[-1]['content'] == '持久工作内容'
            phase = 4
            return response('第一次完成。')
        if phase == 4:
            assert 'read_work_file' in names
            assert '# Demo\n执行演示。' not in payload['messages'][0]['content']
            phase = 5
            return response(name='read_work_file', arguments={'path': 'notes.txt'})
        assert messages[-1]['content'] == '持久工作内容'
        phase = 6
        return response('第二次完成。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        first = client.post(f'/api/conversations/{conversation}/messages', json={'content': '创建文件'})
        assert first.status_code == 200, first.text
        second = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取 Skill 入口'})
        assert second.status_code == 200, second.text


def test_explicit_full_skill_allows_tools_without_load_and_multiple_skills_remain(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式正文直接启用工具；后续消息可读取本会话使用过的另一个 Skill。"""
    catalog = _catalog(tmp_path)
    demo = (catalog / 'demo' / 'SKILL.md').read_text(encoding='utf-8')
    other = (catalog / 'other' / 'SKILL.md').read_text(encoding='utf-8')
    monkeypatch.setenv('SKILLS_DIR', str(catalog))
    phase = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """显式路线不得调用 load_skill，后一条消息应能读取两个已用 Skill。"""
        nonlocal phase
        payload: dict[str, Any] = json.loads(request.content)
        messages = _tool_messages(payload)
        names = [tool['function']['name'] for tool in payload.get('tools', [])]
        if phase == 0:
            assert 'write_work_file' in names
            assert any(item['role'] == 'user' and demo in item['content'] for item in payload['messages'])
            phase = 1
            return response(name='read_skill_file', arguments={'skill': 'demo', 'path': 'SKILL.md'})
        if phase == 1:
            assert messages[-1]['skill'] == 'demo'
            phase = 2
            return response('显式完成。')
        if phase == 2:
            phase = 3
            return response(name='read_skill_file', arguments={'skill': 'other', 'path': 'SKILL.md'})
        assert messages[-1]['skill'] == 'other'
        phase = 4
        return response('多 Skill 完成。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        first = client.post(f'/api/conversations/{conversation}/messages', json={
            'content': f'【Skill：demo】\n\n{demo}\n\n【用户需求】\n显式执行', 'skills': ['demo']})
        assert first.status_code == 200, first.text
        second = client.post(f'/api/conversations/{conversation}/messages', json={
            'content': f'【Skill：other】\n\n{other}\n\n【用户需求】\n再使用 other', 'skills': ['other']})
        assert second.status_code == 200, second.text


def test_read_skill_file_rejects_path_escape(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模型即使被诱导也只能读取已用 Skill 根目录内的相对文件。"""
    catalog = _catalog(tmp_path)
    demo = (catalog / 'demo' / 'SKILL.md').read_text(encoding='utf-8')
    monkeypatch.setenv('SKILLS_DIR', str(catalog))
    phase = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """首次返回越界路径，工具错误后正常结束。"""
        nonlocal phase
        payload: dict[str, Any] = json.loads(request.content)
        messages = _tool_messages(payload)
        if phase == 0:
            phase = 1
            return response(name='read_skill_file', arguments={'skill': 'demo', 'path': '../../etc/passwd'})
        assert '路径' in messages[-1]['error']
        phase = 2
        return response('路径已拒绝。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        sent = client.post(f'/api/conversations/{conversation}/messages', json={
            'content': f'【Skill：demo】\n\n{demo}\n\n【用户需求】\n尝试越界', 'skills': ['demo']})
        assert sent.status_code == 200, sent.text
