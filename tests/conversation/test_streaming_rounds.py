"""通过真实回答入口验证多轮工具执行期间的公开正文保留与最终替换。"""

import json
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest

from tests.auth.support import authenticated_client
from tests.conversation.test_streaming import configure, configure_global_model, wait_message


def round_response(content: str, call_id: str = '') -> httpx.Response:
    """构造正文分片与工具共存的 SSE，覆盖同轮分片和跨轮边界两种追加。"""
    midpoint = len(content) // 2
    frames: list[dict[str, Any]] = [
        {'choices': [{'delta': {'content': content[:midpoint]}}]},
        {'choices': [{'delta': {'content': content[midpoint:]}}]},
    ]
    if call_id:
        frames.append({'choices': [{'delta': {'tool_calls': [{
            'index': 0, 'id': call_id, 'type': 'function',
            'function': {'name': 'get_current_time', 'arguments': '{}'},
        }]}}]})
    frames.append({
        'choices': [{'delta': {}, 'finish_reason': 'tool_calls' if call_id else 'stop'}],
        'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3},
    })
    body = ''.join('data: ' + json.dumps(frame, ensure_ascii=False) + '\n\n' for frame in frames)
    return httpx.Response(200, text=body + 'data: [DONE]\n\n',
                          headers={'content-type': 'text/event-stream'})


@pytest.mark.parametrize('ending', ['complete', 'failed', 'stopped'])
def test_tool_rounds_keep_public_output_until_successful_completion(
    database_url: str, tmp_path: Path, ending: str,
) -> None:
    """后续回合首段前保留确认链接，追加中间正文；成功替换，失败或停止保留。"""
    second_started, third_started = Event(), Event()
    release_second, release_third = Event(), Event()
    calls = 0
    confirmation = '[打开确认页面](/skill-previews/fixture/)'
    accumulated = confirmation + '\n\n正在检查已确认的方案。'

    def provider(request: httpx.Request) -> httpx.Response:
        """在两次后续请求处暂停，检查页面正文不被轮次切换清空或写回模型上下文。"""
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 1:
            return round_response(confirmation, 'clock-first')
        if calls == 2:
            second_started.set()
            assert release_second.wait(10)
            return round_response('正在检查已确认的方案。', 'clock-second')
        assert calls == 3
        assert all(item.get('content') != accumulated for item in payload['messages'])
        third_started.set()
        assert release_third.wait(10)
        if ending == 'failed':
            return httpx.Response(500, json={'error': 'private-provider-detail'})
        return round_response('最终交付说明。')

    configure_global_model(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        try:
            started = client.post(f'/api/conversations/{conversation}/runs', json={'content': '执行并展示进度'})
            assert started.status_code == 202
            assert second_started.wait(10)
            wait_message(client, conversation, 'generating', confirmation, timeout=1)
            release_second.set()
            assert third_started.wait(10)
            pending = wait_message(client, conversation, 'generating', accumulated, timeout=1)
            if ending == 'stopped':
                stopped = client.post(f"/api/conversations/{conversation}/messages/{pending['id']}/stop")
                assert stopped.status_code == 200
        finally:
            release_second.set()
            release_third.set()
        expected = '最终交付说明。' if ending == 'complete' else accumulated
        answer = wait_message(client, conversation, ending, expected)
        if ending == 'failed':
            assert 'HTTP 500' in answer['error']
            assert 'private-provider-detail' not in str(answer)
