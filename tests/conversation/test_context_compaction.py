"""通过聊天和模型传输接口验证上下文压缩与后续摘要续用。"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.agent.test_multi_agent import response
from tests.auth.support import authenticated_client, set_global_settings


def test_compacted_context_survives_restart_without_changing_chat_history(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """旧回答压缩成摘要后，下一条及重启后继续使用摘要，原消息仍完整可读。"""
    catalog = tmp_path / 'empty-catalog'
    catalog.mkdir()
    monkeypatch.setenv('SKILLS_DIR', str(catalog))
    observed: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    long_answer = '已核实的旧资料片段。' * 900

    def provider(request: httpx.Request) -> httpx.Response:
        """外部模型按摘要请求和普通回答分别给出独立的确定结果。"""
        payload = json.loads(request.content)
        if '会话上下文压缩' in str(payload['messages'][0]['content']):
            summaries.append(payload)
            return response('用户目标：完成产品汇报；决定：蓝白配色；已核实旧资料；待办：下一步排版。')
        observed.append(payload)
        return response(long_answer if len(observed) == 1 else '已按此前决定继续。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 32768,
        'output_reserve_tokens': 1024})
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        assert client.post(f'/api/conversations/{conversation}/messages', json={'content': '按蓝白配色准备产品汇报'}).status_code == 200
        sent = client.post(f'/api/conversations/{conversation}/messages', json={'content': '继续排版，标题保持原样'})
        assert sent.status_code == 200, sent.text
        assert summaries, '上下文接近阈值时应先生成摘要'
        request_text = json.dumps(observed[-1]['messages'], ensure_ascii=False)
        assert '用户目标：完成产品汇报' in request_text
        assert long_answer not in request_text
        assert observed[-1]['messages'][-1]['content'] == '继续排版，标题保持原样'
        assert sent.json()['messages'][1]['content'] == long_answer
    before = len(summaries)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        sent = client.post(f'/api/conversations/{conversation}/messages', json={'content': '现在检查标题'})
        assert sent.status_code == 200, sent.text
        text = json.dumps(observed[-1]['messages'], ensure_ascii=False)
        assert text.count('用户目标：完成产品汇报') == 1
        assert long_answer not in text
        assert len(summaries) == before
        assert len(sent.json()['messages']) == 6


def test_failed_summary_keeps_original_records_and_retry_can_compact(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """摘要服务失败不删旧正文，修复服务后重试相同用户消息可完成压缩。"""
    catalog = tmp_path / 'empty-catalog'
    catalog.mkdir()
    monkeypatch.setenv('SKILLS_DIR', str(catalog))
    broken = True
    answers = 0
    original = '需要保留的完整旧内容。' * 900

    def provider(request: httpx.Request) -> httpx.Response:
        """只在摘要阶段模拟外部失败，普通回答仍按原模型协议输出。"""
        nonlocal answers
        payload = json.loads(request.content)
        if '会话上下文压缩' in payload['messages'][0]['content']:
            if broken:
                return httpx.Response(400, json={'error': {'message': 'summary unavailable'}})
            return response('此前已确认内容与待办已保留。')
        answers += 1
        return response(original if answers == 1 else '压缩后已继续。')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1',
        'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 32768, 'output_reserve_tokens': 1024})
    from tests.conversation.test_streaming import wait_message
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        assert client.post(f'/api/conversations/{conversation}/messages', json={'content': '先整理资料'}).status_code == 200
        failed = client.post(f'/api/conversations/{conversation}/messages', json={'content': '继续处理'})
        assert failed.status_code == 502
        messages = client.get(f'/api/conversations/{conversation}').json()['messages']
        assert messages[1]['content'] == original
        assert '压缩' in messages[-1]['error']
        broken = False
        retried = client.post(f"/api/conversations/{conversation}/messages/{messages[-1]['id']}/retry")
        assert retried.status_code == 202, retried.text
        wait_message(client, conversation, 'complete', '压缩后已继续。')
        final = client.get(f'/api/conversations/{conversation}').json()['messages']
        assert [item['content'] for item in final if item['role'] == 'user'] == ['先整理资料', '继续处理']
        assert final[1]['content'] == original
