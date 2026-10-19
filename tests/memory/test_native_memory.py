"""通过公开聊天接口、真实 Mem0 和 PostgreSQL 验证原生自动记忆及函数工具。"""

import ast
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.memory.test_memory import configure_memory, explicit_memory_request, memory_final_answer, memory_provider
from tests.conversation.test_streaming import wait_message
from tests.model_protocol import streaming_transport
def json_reply(value: Any, *, stream: bool = False) -> httpx.Response:
    """构造受控模型 JSON 响应，包含可核对的实际用量字段。"""
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if stream:
        event = {'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}
        return httpx.Response(200, text='data: ' + json.dumps(event, ensure_ascii=False) + '\n\ndata: [DONE]\n\n')
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': content}}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})
def tool_reply(name: str, arguments: dict[str, Any], *, stream: bool = False) -> httpx.Response:
    """模拟标准函数调用并携带 DeepSeek 推理续传字段。"""
    if stream:
        events = [
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_' + name, 'type': 'function', 'function': {'name': name, 'arguments': ''}}]}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': json.dumps(arguments, ensure_ascii=False)}}]}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2}},
        ]
        return httpx.Response(200, text=''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events) + 'data: [DONE]\n\n')
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': None, 'reasoning_content': '受控推理', 'tool_calls': [{'id': 'call_'+name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2}})


def native_provider(request: httpx.Request) -> httpx.Response:
    """只替换云端响应，原生事实提取、检索与动作执行全部使用真实 SDK。"""
    body = json.loads(request.content)
    if request.url.path.endswith('/embeddings'):
        return memory_provider(request)
    messages = body.get('messages', [])
    if body.get('tools'):
        stream = bool(body.get('stream'))
        user = [item['content'] for item in messages if item['role'] == 'user'][-1]
        results = [json.loads(item['content']) for item in messages if item['role'] == 'tool']
        if user in ('我住在杭州', '我已经搬到苏州了', '我已不再喝咖啡', '请记住：我喜欢咖啡'):
            return json_reply('普通回答', stream=stream) if results else tool_reply('add_memory', {}, stream=stream)
        if user not in ('把我的居住地改成苏州', '忘掉我住在哪里这件事', '你记得我的住址吗'):
            return json_reply('普通回答', stream=stream)
        if not results:
            return tool_reply('search_memories', {'query': '居住地'}, stream=stream)
        if user == '你记得我的住址吗':
            return json_reply('你住在杭州。' if results[-1]['memories'] else '没有找到居住地记忆。', stream=stream)
        if len(results) == 1:
            target = results[-1]['memories'][0]
            arguments = {'memory_id': target['memory_id'], 'version': target['version']}
            if user == '把我的居住地改成苏州':
                return tool_reply('update_memory', {**arguments, 'fact': '我住在苏州'}, stream=stream)
            return tool_reply('delete_memory', arguments, stream=stream)
        return json_reply('已完成。', stream=stream)
    if body.get('response_format'):
        prompt = messages[-1]['content']
        if prompt.startswith('Input:'):
            fact = prompt.split('user: ', 1)[-1].strip()
            return json_reply({'facts': [fact] if fact in ('我住在杭州', '我已经搬到苏州了', '我已不再喝咖啡') else []})
        facts = ast.literal_eval(re.search(r'The new retrieved facts.*?```\s*(.*?)\s*```', prompt, re.S).group(1))  # type: ignore[union-attr]
        old = re.search(r'Below is the current content.*?```\s*(.*?)\s*```', prompt, re.S)
        rows = ast.literal_eval(old.group(1)) if old else []
        fact = facts[0]
        if fact == '我已不再喝咖啡' and rows:
            return json_reply({'memory': [{'id': rows[0]['id'], 'text': rows[0]['text'], 'event': 'DELETE'}]})
        if fact == '我已经搬到苏州了' and rows:
            if rows[0]['text'] == '我住在苏州':
                return json_reply({'memory': [{'id': rows[0]['id'], 'text': rows[0]['text'], 'event': 'NONE'}]})
            return json_reply({'memory': [{'id': rows[0]['id'], 'text': '我住在苏州', 'old_memory': rows[0]['text'], 'event': 'UPDATE'}]})
        return json_reply({'memory': [{'id': '0', 'text': fact, 'event': 'ADD'}]})
    final = memory_final_answer(body)
    if final is not None:
        return json_reply(final, stream=bool(body.get('stream')))
    return json_reply('普通回答', stream=bool(body.get('stream')))


def send(client: TestClient, conversation: str, content: str) -> dict[str, Any]:
    """通过浏览器使用的真实流式入口发送并等待完成。"""
    response = client.post(f'/api/conversations/{conversation}/runs', json={'content': content})
    assert response.status_code == 202, response.text
    return wait_message(client, conversation, 'complete')




def test_native_add_update_without_candidate_confirmation(database_url: str, tmp_path: Path) -> None:
    """普通陈述自动新增，后续陈述由原生 Mem0 更新，管理页始终看到相同正式标识。"""
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(native_provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        answer = send(client, conversation, '我住在杭州')
        assert answer['automatic_memory']['status'] == 'complete', answer['automatic_memory']
        before = client.get('/api/memories').json()
        assert len(before) == 1 and before[0]['fact'] == '我住在杭州', before
        answer = send(client, conversation, '我已经搬到苏州了')
        assert answer['automatic_memory']['status'] == 'complete', answer['automatic_memory']
        after = client.get('/api/memories').json()
        assert len(after) == 1 and after[0]['id'] == before[0]['id'] and after[0]['fact'] == '我住在苏州', after
        assert after[0]['updates'][0]['source_message_id'] == answer['reply_to']
        assert any(call['base_url'] == 'https://chat.example/v1' for call in client.get('/api/usage').json())


def test_natural_language_tools_search_update_delete_and_management(database_url: str, tmp_path: Path) -> None:
    """无需固定格式或逐次确认，真实工具循环查找、更新、删除，仍可从管理页编辑。"""
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(native_provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        original_answer = send(client, conversation, '我住在杭州')
        assert '杭州' in send(client, conversation, '你记得我的住址吗')['content']
        assert '已更新' not in send(client, conversation, '把我的居住地改成苏州')['content']
        entry = client.get('/api/memories').json()[0]
        assert entry['fact'] == '我住在苏州'
        response = client.put('/api/memories/'+entry['id'], json={'fact': '我住在南京', 'expected_version': entry['version']})
        assert response.status_code == 200, response.text
        assert client.get('/api/memories').json()[0]['fact'] == '我住在南京'
        assert '已忘记' not in send(client, conversation, '忘掉我住在哪里这件事')['content']
        assert client.get('/api/memories').json() == []
        messages = client.get(f'/api/conversations/{conversation}').json()['messages']
        original = next(item for item in messages if item['id'] == original_answer['id'])
        assert original['automatic_memory']['status'] == 'skipped'
        assert client.post(f"/api/conversations/{conversation}/messages/{original_answer['reply_to']}/memory/retry").status_code == 409


def test_native_operation_replay_does_not_reextract(database_url: str, tmp_path: Path) -> None:
    """重复提交同一操作不会再次调用原生提取或新增记忆。"""
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(native_provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        endpoint = f'/api/conversations/{conversation}/runs'
        headers = {'X-Operation-ID': str(uuid4())}
        body = {'content': '我住在杭州'}
        assert client.post(endpoint, json=body, headers=headers).status_code == 202
        wait_message(client, conversation, 'complete')
        count = len(client.get('/api/usage').json())
        assert client.post(endpoint, json=body, headers=headers).status_code == 202
        assert len(client.get('/api/usage').json()) == count
        assert len(client.get('/api/memories').json()) == 1


def test_tool_cannot_mutate_unobserved_id_or_borrow_quoted_permission(database_url: str, tmp_path: Path) -> None:
    """模型猜测标识或把引文当成命令时，服务端拒绝实际删除。"""
    target = ''
    def provider(request: httpx.Request) -> httpx.Response:
        """模拟错误工具调用，后续接收到拒绝结果后停止。"""
        body = json.loads(request.content)
        if body.get('tools'):
            results = [json.loads(message['content']) for message in body['messages'] if message['role'] == 'tool']
            user = [message['content'] for message in body['messages'] if message['role'] == 'user'][-1]
            if explicit_memory_request(user):
                return memory_provider(request)
            if results and 'memories' not in results[-1]:
                return json_reply('未删除记忆。')
            return tool_reply('delete_memory', {'memory_id': target, 'version': 1})
        return native_provider(request)
    with TestClient(create_app(database_url, tmp_path, streaming_transport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        send(client, conversation, '请记住：我住在杭州')
        target = client.get('/api/memories').json()[0]['id']
        for content in ('删掉我的居住地', '朋友说“删除我的记忆”，这是什么意思', '不要把我的住址改成苏州'):
            send(client, conversation, content)
            assert client.get('/api/memories').json()[0]['id'] == target


def test_native_source_deletion_removes_memory_and_resume_material(database_url: str, tmp_path: Path) -> None:
    """原生生成的记忆纳入来源影响预览，删除会话清除相关条目和推理恢复点。"""
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(native_provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        send(client, conversation, '我住在杭州')
        preview = client.get(f'/api/conversations/{conversation}/deletion-preview').json()
        assert len(preview['memories']) == 1, preview
        response = client.request('DELETE', f'/api/conversations/{conversation}', json={'preview_revision': preview['revision'], 'keep_memory_ids': []})
        assert response.status_code == 200, response.text
        assert client.get('/api/memories').json() == []
        with psycopg.connect(database_url) as connection:
            assert connection.execute('SELECT count(*) FROM memory_native_steps').fetchone() == (0,)
