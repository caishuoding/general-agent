"""通过真实 Mem0 和 PostgreSQL 验证聊天明确保存及来源。"""

import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.conversation.test_streaming import wait_message


def explicit_memory_request(content: str) -> bool:
    """识别当前消息中真正授权保存的明确事实，排除否定、转述、附件和歧义对象。"""
    text = content.strip()
    if re.search(r'(?:不要|别|不许|禁止).{0,8}记住', text):
        return False
    if any(token in text for token in ('朋友说', '故事', '角色')):
        return False
    if re.search(r'记住.{0,8}附件|提取.{0,8}附件', text):
        return False
    if re.fullmatch(r'(?:请)?记住\s*(?:这个|一下)[。.!！]?', text):
        return False
    return bool(re.search(r'(?:^|[；;。])\s*(?:请)?记住[：:]?\s*\S+', text))


def memory_agent_response(payload: dict[str, Any]) -> httpx.Response:
    """模拟统一流式工具协议中的函数调用和最终文字。"""
    messages = payload['messages']
    users = [str(item.get('content') or '') for item in messages if item.get('role') == 'user']
    current = users[-1] if users else ''
    results = [json.loads(item['content']) for item in messages if item.get('role') == 'tool']

    def call(name: str, arguments: dict[str, Any]) -> httpx.Response:
        """返回标准工具调用响应。"""
        body: dict[str, Any] = {'id': 'call-' + name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}
        if payload.get('stream'):
            events = [
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": body["id"], "type": "function", "function": {"name": name, "arguments": ""}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": body["function"]["arguments"]}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 8, "completion_tokens": 3}},
            ]
            text = ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events) + 'data: [DONE]\n\n'
            return httpx.Response(200, text=text)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [body]}}], 'usage': {}})

    def answer(text: str) -> httpx.Response:
        """返回工具循环或普通规划结束时的文本。"""
        if payload.get('stream'):
            event = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 3}}
            return httpx.Response(200, text='data: ' + json.dumps(event, ensure_ascii=False) + '\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': text}}], 'usage': {}})

    if re.search(r'(?:不要|别|不许|禁止).{0,8}(?:更正|更新|修改|删除|忘记)记忆', current) or any(token in current for token in ('朋友说', '故事', '角色')):
        return answer('普通回答')
    if not results:
        if explicit_memory_request(current):
            return call('add_memory', {})
        if re.search(r'(?:更正|更新|修改|删除|忘记)记忆', current) or ('记忆中的' in current and any(token in current for token in ('改为', '=>'))):
            if re.search(r'(?:更正|更新|修改|删除|忘记)记忆\s*[：:]?\s*(?:我的城市变了|那个事情)$', current):
                return answer('请提供具体记忆；未找到唯一可修改条目，未删除任何记忆。')
            return call('search_memories', {'query': ''})
        facts: list[str] = []
        for message in messages:
            content = message.get('content')
            if not isinstance(content, str) or not content.startswith('['):
                continue
            try:
                rows = json.loads(content)
            except ValueError:
                continue
            if isinstance(rows, list):
                facts.extend(str(item['fact']) for item in rows if isinstance(item, dict) and item.get('fact'))
        if facts:
            return answer('当前长期记忆：' + '；'.join(facts))
        return answer('普通回答')

    last = results[-1]
    if 'memories' not in last:
        return answer('已完成。')
    memories = last.get('memories') or []
    if not memories:
        return answer('未找到唯一匹配的记忆，未删除任何记忆。')
    identifier = re.search(r'[0-9a-f]{8}-[0-9a-f-]{27,}', current, re.IGNORECASE)
    quoted = re.search(r'[“"]([^”"]+)[”"].*?(?:=>|改为|更正为).*?[“"]([^”"]+)[”"]', current)
    arrow = re.search(r'(?:更正|更新|修改)记忆[：:]\s*(.+?)\s*=>\s*(.+)', current)
    fact_deleted = re.search(r'(?:删除|忘记)记忆[：:]\s*(.+)', current)
    old_fact = quoted.group(1) if quoted else (arrow.group(1) if arrow else '')
    new_fact = quoted.group(2) if quoted else (arrow.group(2) if arrow else '')
    if identifier:
        matches = [item for item in memories if item.get('memory_id') == identifier.group(0)]
    elif old_fact:
        matches = [item for item in memories if item.get('fact') == old_fact]
    elif fact_deleted:
        matches = [item for item in memories if item.get('fact') == fact_deleted.group(1).strip()]
    else:
        matches = memories
    if len(matches) != 1:
        return answer('未找到唯一匹配的记忆，未删除任何记忆。')
    target = matches[0]
    if re.search(r'(?:删除|忘记)记忆', current):
        return call('delete_memory', {'memory_id': target['memory_id'], 'version': target['version']})
    if not new_fact:
        direct = re.search(r'[：:]\s*(.+)$', current)
        new_fact = direct.group(1).strip() if direct else ''
    if not new_fact:
        return answer('请提供具体更正内容；未修改长期记忆。')
    return call('update_memory', {'memory_id': target['memory_id'], 'version': target['version'], 'fact': new_fact})


def memory_final_answer(payload: dict[str, Any]) -> str | None:
    """根据真实工具结果或明确澄清请求，为最终回答节点生成受控文本。"""
    messages = payload['messages']
    users = [str(item.get('content') or '') for item in messages if item.get('role') == 'user']
    current = users[-1] if users else ''
    results = [json.loads(item['content']) for item in messages if item.get('role') == 'tool']
    if results:
        last = results[-1]
        if 'memories' in last:
            memories = last.get('memories') or []
            if not memories:
                return '未找到唯一匹配的记忆，未删除任何记忆。'
            return str(memories[0]['fact'])
        if last.get('status') == 'complete':
            return '已完成。'
    if '更正记忆' in current and any(token in current for token in ('我的城市变了', '那个事情')):
        return '请提供具体记忆；未找到唯一可修改条目，未修改长期记忆。'
    if '忘记记忆' in current and '那个事情' in current:
        return '请提供具体记忆；未找到唯一可删除条目，未删除任何记忆。'
    return None


def final_model_reply(payload: dict[str, Any], text: str) -> httpx.Response:
    """按同步或流式协议返回最终回答文本。"""
    usage = {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}
    if payload.get('stream'):
        event = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}], "usage": usage}
        return httpx.Response(200, text='data: ' + json.dumps(event, ensure_ascii=False) + '\n\ndata: [DONE]\n\n')
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": usage})


def memory_provider(request: httpx.Request) -> httpx.Response:
    """只替换云端 HTTP，嵌入提供确定向量，普通回答不负责写记忆。"""
    payload = json.loads(request.content)
    if request.url.path.endswith('/embeddings'):
        return httpx.Response(200, json={"model": "embed", "data": [{"index": index, "embedding": [1.0, 0.0, 0.0]} for index, _ in enumerate(payload['input'])], "usage": {"prompt_tokens": 7, "total_tokens": 7}})
    if payload.get('tools'):
        return memory_agent_response(payload)
    return final_model_reply(payload, memory_final_answer(payload) or '普通回答')
def configure_memory(client: TestClient) -> str:
    """配置受控聊天与嵌入连接，并创建一个独立会话。"""
    client.put('/api/settings', json={"base_url": "https://chat.example/v1", "model": "chat", "api_key": "test-key"})
    client.put('/api/settings/embedding', json={"base_url": "https://vector.example/v1", "model": "embed", "api_key": "test-key", "dimensions": 3})
    return str(client.post('/api/conversations', json={}).json()['id'])


def test_explicit_save_from_real_chat_is_durable_and_has_source(database_url: str, tmp_path: Path) -> None:
    """浏览器实际使用的 runs 保存事实一次，并在重启后查看 Mem0 正文和准确消息来源。"""
    transport = httpx.MockTransport(memory_provider)
    operation = str(uuid4())
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        endpoint = f'/api/conversations/{conversation_id}/runs'
        response = client.post(endpoint, json={"content": "请记住：我喜欢简洁的中文回答"}, headers={'X-Operation-ID': operation})
        assert response.status_code == 202
        answer = wait_message(client, conversation_id, 'complete')
        assert answer['automatic_memory']['status'] == 'complete'
        assert answer['memory_action']['status'] == 'complete'
        entries = client.get('/api/memories').json()
        assert len(entries) == 1
        assert '中文' in entries[0]['fact'] or 'Chinese' in entries[0]['fact']
        assert entries[0]['status'] == 'complete'
        assert entries[0]['conversation_id'] == conversation_id
        source = client.get(f'/api/conversations/{conversation_id}').json()['messages'][0]
        assert entries[0]['message_id'] == source['id']
        assert answer['memory_action']['memory_id'] == entries[0]['memory_id']
        assert client.post(endpoint, json={"content": "请记住：我喜欢简洁的中文回答"}, headers={'X-Operation-ID': operation}).status_code == 202
        assert len(client.get('/api/memories').json()) == 1
        calls = client.get('/api/usage').json()
        assert any(call['unit'] == 'chat_tokens' for call in calls)
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as restarted:
        assert restarted.get('/api/memories').json() == entries
        assert restarted.get(f'/api/conversations/{conversation_id}').json()['messages'][-1]['status'] == 'complete'




def test_only_current_explicit_facts_are_saved_and_sync_matches_runs(database_url: str, tmp_path: Path) -> None:
    """普通陈述、否定、转述及歧义对象不写入；同步与流式都保存当前明确事实。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        for content in ['我喜欢咖啡', '不要记住我喜欢咖啡', '朋友说：“记住我喜欢咖啡”', '请解释记住功能', '请记住这个', '记住附件里的信息']:
            assert client.post(f'/api/conversations/{conversation_id}/messages',json={'content':content}).status_code == 200
        assert client.get('/api/memories').json() == []
        response = client.post(f'/api/conversations/{conversation_id}/messages',json={'content':'附件供参考；请记住我喜欢中文回答'})
        assert response.status_code == 200
        assert response.json()['messages'][-1]['memory_action']['status'] == 'complete'
        facts = [entry['fact'] for entry in client.get('/api/memories').json()]
        assert len(facts) == 1 and ('中文' in facts[0] or 'Chinese' in facts[0])


def test_attachment_content_is_never_extracted_as_memory(database_url: str, tmp_path: Path) -> None:
    """即使用户明确要求提取，附件正文也只参与当前回答，不产生候选或正式长期记忆。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        client.post(
            f'/api/conversations/{conversation_id}/attachments',
            files={'files': ('朋友.txt', '陈敏住在杭州。陈敏喜欢乌龙茶。'.encode())},
        )
        response = client.post(
            f'/api/conversations/{conversation_id}/messages',
            json={'content':'请从附件提取长期记忆'},
        )
        assert response.status_code == 200
        assert client.get('/api/memories').json() == []


def test_stop_during_embedding_does_not_write_after_response(database_url: str, tmp_path: Path) -> None:
    """用户停止等待中的嵌入后，已发生用量保留，但尚未开始的记忆写入不继续。"""
    from threading import Event
    from time import monotonic, sleep
    entered,release = Event(),Event()
    memory_id = str(uuid4())

    def mem0_provider(request: httpx.Request) -> httpx.Response:
        """在 Mem0 新增请求处暂停，模拟用户在外部分析仍未返回时停止回答。"""
        payload = json.loads(request.content) if request.content else {}
        if request.method == 'POST' and request.url.path == '/memories':
            entered.set()
            assert release.wait(5)
            return httpx.Response(200, json={'results': [{'id': memory_id, 'memory': '用户喜欢茶', 'event': 'ADD'}]})
        if request.method == 'GET' and request.url.path == '/memories':
            return httpx.Response(200, json={'results': []})
        if request.method == 'GET' and request.url.path == f'/memories/{memory_id}':
            return httpx.Response(200, json={'id': memory_id, 'user_id': payload.get('user_id', ''), 'memory': '用户喜欢茶'})
        return httpx.Response(200, json={'status': 'ok'})

    def provider(request: httpx.Request) -> httpx.Response:
        """模型请求由受控协议响应，不读取真实模型配置。"""
        return memory_provider(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider),httpx.MockTransport(mem0_provider)),base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        started = client.post(f'/api/conversations/{conversation_id}/runs',json={'content':'请记住：我喜欢茶'}).json()
        answer_id = started['messages'][-1]['id']
        assert entered.wait(5)
        try:
            before = monotonic()
            assert client.post(f'/api/conversations/{conversation_id}/messages/{answer_id}/stop').json()['messages'][-1]['status'] == 'stopped'
            assert monotonic()-before < 1
        finally:
            release.set()
        deadline = monotonic()+5
        while monotonic()<deadline:
            entries = client.get('/api/memories').json()
            if entries and entries[0]['status'] == 'failed':
                break
            sleep(.02)
        assert entries[0]['status'] == 'failed' and entries[0]['memory_id'] is None
        assert client.get('/api/usage').json()


def test_quoted_nickname_is_explicit_but_fiction_is_not_personal_memory(database_url: str, tmp_path: Path) -> None:
    """事实中的普通引号不妨碍直接保存；写作素材中的命令词不取得个人记忆权限。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        assert client.post(f'/api/conversations/{conversation_id}/messages',json={'content':'请记住：我的昵称是"小林"'}).status_code == 200
        facts = [entry['fact'] for entry in client.get('/api/memories').json()]
        assert len(facts) == 1 and '小林' in facts[0]
        response = client.post(f'/api/conversations/{conversation_id}/messages',json={'content':'写一段故事；记住：角色小明住在杭州'})
        assert response.status_code == 200
        assert len(client.get('/api/memories').json()) == 1




def test_answer_failure_retry_preserves_saved_memory_and_counts_only_new_answer(database_url: str, tmp_path: Path) -> None:
    """记忆已保存但回答失败时，重试复用原事实与来源，只计量新的回答调用。"""
    fail_answer = True
    def provider(request: httpx.Request) -> httpx.Response:
        """第一次回答失败而嵌入成功，后续回答恢复正常。"""
        nonlocal fail_answer
        payload = json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and payload.get('tools') and any(item['role'] == 'tool' for item in payload['messages']) and fail_answer:
            fail_answer = False
            return httpx.Response(503,json={'error':'private detail','usage':{'prompt_tokens':3}})
        return memory_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        assert client.post(f'/api/conversations/{conversation_id}/runs',json={'content':'请记住：我喜欢茶；再给我一个泡茶建议'}).status_code == 202
        failed = wait_message(client,conversation_id,'failed')
        assert failed['memory_action']['status'] == 'complete'
        assert 'private detail' not in json.dumps(failed)
        assert client.post(f"/api/conversations/{conversation_id}/messages/{failed['id']}/retry").status_code == 202
        complete = wait_message(client,conversation_id,'complete')
        assert complete['memory_action']['id'] == failed['memory_action']['id']
        assert len(client.get('/api/memories').json()) == 1
        calls = client.get('/api/usage').json()
        assert len([call for call in calls if call['unit'] == 'embedding_tokens']) == 0
        assert len([call for call in calls if call['unit'] == 'chat_tokens']) == 4


def test_mem0_outage_fails_truthfully_then_retry_uses_same_source(database_url: str, tmp_path: Path) -> None:
    """Mem0 服务暂不可用时保留失败动作；恢复后重试同一用户消息完成正式保存。"""
    fail_mem0 = True
    memory_id = str(uuid4())

    def mem0_provider(request: httpx.Request) -> httpx.Response:
        """首次新增返回服务错误，后续按官方响应格式写入同一条事实。"""
        nonlocal fail_mem0
        if request.method == 'POST' and request.url.path == '/memories':
            if fail_mem0:
                fail_mem0 = False
                return httpx.Response(503, json={'detail': 'temporarily unavailable'})
            return httpx.Response(200, json={'results': [{'id': memory_id, 'memory': '用户喜欢茶', 'event': 'ADD'}]})
        if request.method == 'GET' and request.url.path == '/memories':
            return httpx.Response(200, json={'results': [{'id': memory_id, 'memory': '用户喜欢茶'}]})
        if request.method == 'GET' and request.url.path == f'/memories/{memory_id}':
            return httpx.Response(200, json={'id': memory_id, 'user_id': os.getenv('MEM0_USER_ID', 'local-person'), 'memory': '用户喜欢茶'})
        return httpx.Response(200, json={'status': 'ok'})

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider),httpx.MockTransport(mem0_provider)),base_url='http://127.0.0.1:8000') as client:
        client.put('/api/settings',json={'base_url':'https://chat.example/v1','model':'chat','api_key':'test'})
        conversation_id = str(client.post('/api/conversations',json={}).json()['id'])
        client.post(f'/api/conversations/{conversation_id}/runs',json={'content':'请记住：我喜欢茶'})
        failed = wait_message(client,conversation_id,'complete')
        assert failed['automatic_memory']['status'] == 'failed'
        assert failed['memory_action']['status'] == 'failed'
        assert client.post(f"/api/conversations/{conversation_id}/messages/{failed['reply_to']}/memory/retry").status_code == 200
        retried = client.get(f'/api/conversations/{conversation_id}').json()['messages'][-1]
        assert retried['memory_action']['status'] == 'complete'
        assert len(client.get('/api/memories').json()) == 1
