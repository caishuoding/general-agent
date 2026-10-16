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
from test_memory import configure_memory, memory_provider
from test_streaming import wait_message


def json_reply(value: Any) -> httpx.Response:
    """构造受控模型 JSON 响应，包含可核对的实际用量字段。"""
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': content}}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})


def tool_reply(name: str, arguments: dict[str, Any]) -> httpx.Response:
    """模拟标准函数调用并携带 DeepSeek 推理续传字段。"""
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': None, 'reasoning_content': '受控推理', 'tool_calls': [{'id': 'call_'+name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}}], 'usage': {'prompt_tokens': 3, 'completion_tokens': 2}})


def native_provider(request: httpx.Request) -> httpx.Response:
    """只替换云端响应，原生事实提取、检索与动作执行全部使用真实 SDK。"""
    body = json.loads(request.content)
    if request.url.path.endswith('/embeddings') or body.get('stream'):
        return memory_provider(request)
    messages = body.get('messages', [])
    if body.get('tools'):
        user = next(item['content'] for item in messages if item['role'] == 'user')
        if user not in ('把我的居住地改成苏州', '忘掉我住在哪里这件事', '你记得我的住址吗'):
            return json_reply('NO_MEMORY_ACTION')
        results = [json.loads(item['content']) for item in messages if item['role'] == 'tool']
        if not results:
            return tool_reply('search_memories', {'query': '居住地'})
        assert all('reasoning_content' in item for item in messages if item['role'] == 'assistant')
        if user == '你记得我的住址吗':
            return json_reply('你住在杭州。' if results[-1]['memories'] else '没有找到居住地记忆。')
        if len(results) == 1:
            target = results[-1]['memories'][0]
            arguments = {'memory_id': target['memory_id'], 'version': target['version']}
            if user == '把我的居住地改成苏州':
                return tool_reply('update_memory', {**arguments, 'fact': '我住在苏州'})
            return tool_reply('delete_memory', arguments)
        return json_reply('已更新记忆。' if user == '把我的居住地改成苏州' else '已忘记居住地，原聊天保留。')
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
    return json_reply('普通回答')


def send(client: TestClient, conversation: str, content: str) -> dict[str, Any]:
    """通过浏览器使用的真实流式入口发送并等待完成。"""
    response = client.post(f'/api/conversations/{conversation}/runs', json={'content': content})
    assert response.status_code == 202, response.text
    return wait_message(client, conversation, 'complete')


def wait_budget_step(client: TestClient, conversation: str) -> dict[str, Any]:
    """等待一次恢复到下一暂停或终态，避免把尚在生成的完成回执误当新的暂停。"""
    from time import monotonic, sleep
    deadline = monotonic() + 5
    while monotonic() < deadline:
        current = client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
        if current['status'] != 'generating':
            return dict(current)
        sleep(.02)
    raise AssertionError('预算步骤未在预期时间内结束。')


def test_native_add_update_without_candidate_confirmation(database_url: str, tmp_path: Path) -> None:
    """普通陈述自动新增，后续陈述由原生 Mem0 更新，管理页始终看到相同正式标识。"""
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(native_provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        answer = send(client, conversation, '我住在杭州')
        assert answer['memory_candidates'] == []
        assert answer['automatic_memory']['status'] == 'complete', answer['automatic_memory']
        before = client.get('/api/memories').json()
        assert len(before) == 1 and before[0]['fact'] == '我住在杭州', before
        answer = send(client, conversation, '我已经搬到苏州了')
        assert answer['automatic_memory']['status'] == 'complete', answer['automatic_memory']
        after = client.get('/api/memories').json()
        assert len(after) == 1 and after[0]['id'] == before[0]['id'] and after[0]['fact'] == '我住在苏州', after
        assert after[0]['updates'][0]['source_message_id'] == answer['reply_to']
        assert any(call['base_url'] == 'https://chat.example/v1' for call in client.get('/api/usage').json())


def test_native_delete_purges_history_without_deleting_chat(database_url: str, tmp_path: Path) -> None:
    """原生 DELETE 决策执行完整忘记，原聊天保留且不生成确认卡片。"""
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(native_provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        send(client, conversation, '请记住：我喜欢咖啡')
        old = client.get('/api/memories').json()[0]
        answer = send(client, conversation, '我已不再喝咖啡')
        assert answer['automatic_memory']['status'] == 'complete', answer['automatic_memory']
        assert client.get('/api/memories').json() == []
        with psycopg.connect(database_url) as connection:
            assert connection.execute('SELECT count(*) FROM mem0_history WHERE memory_id=%s', (old['memory_id'],)).fetchone() == (0,)
        assert '我喜欢咖啡' in json.dumps(client.get(f'/api/conversations/{conversation}').json(), ensure_ascii=False)


def test_natural_language_tools_search_update_delete_and_management(database_url: str, tmp_path: Path) -> None:
    """无需固定格式或逐次确认，真实工具循环查找、更新、删除，仍可从管理页编辑。"""
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(native_provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        original_answer = send(client, conversation, '我住在杭州')
        assert '杭州' in send(client, conversation, '你记得我的住址吗')['content']
        assert '已更新' in send(client, conversation, '把我的居住地改成苏州')['content']
        entry = client.get('/api/memories').json()[0]
        assert entry['fact'] == '我住在苏州'
        response = client.put('/api/memories/'+entry['id'], json={'fact': '我住在南京', 'expected_version': entry['version']})
        assert response.status_code == 200, response.text
        assert client.get('/api/memories').json()[0]['fact'] == '我住在南京'
        assert '已忘记' in send(client, conversation, '忘掉我住在哪里这件事')['content']
        assert client.get('/api/memories').json() == []
        messages = client.get(f'/api/conversations/{conversation}').json()['messages']
        original = next(item for item in messages if item['id'] == original_answer['id'])
        assert original['automatic_memory']['status'] == 'skipped'
        assert client.post(f"/api/conversations/{conversation}/messages/{original_answer['reply_to']}/memory/retry").status_code == 409


def test_native_empty_and_invalid_json_do_not_claim_saved(database_url: str, tmp_path: Path) -> None:
    """空事实合法不保存，畸形原生输出明确失败且不妨碍普通回答。"""
    broken = False
    def provider(request: httpx.Request) -> httpx.Response:
        """仅对原生推理安排一次畸形 JSON。"""
        if broken and json.loads(request.content).get('response_format'):
            return json_reply('bad json')
        return native_provider(request)
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        answer = send(client, conversation, '你好')
        assert answer['automatic_memory']['status'] == 'complete'
        assert client.get('/api/memories').json() == []
        broken = True
        answer = send(client, conversation, '我住在杭州')
        assert answer['automatic_memory']['status'] == 'failed'
        assert client.get('/api/memories').json() == []
        broken = False
        endpoint = f"/api/conversations/{conversation}/messages/{answer['reply_to']}/memory/retry"
        assert client.post(endpoint).status_code == 200
        assert client.get('/api/memories').json()[0]['fact'] == '我住在杭州'
        count = len(client.get('/api/usage').json())
        assert client.post(endpoint).status_code == 200
        assert len(client.get('/api/usage').json()) == count


def test_native_partial_update_retry_repairs_same_entry(database_url: str, tmp_path: Path) -> None:
    """真实数据库历史写入失败后，自动记忆重试修复同一更正，不新增副本或重复加版本。"""
    transport = httpx.MockTransport(native_provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        send(client, conversation, '我住在杭州')
        original = client.get('/api/memories').json()[0]
        with psycopg.connect(database_url) as connection:
            # 注入真实历史表故障，保留原生 SDK 已写入的向量以验证恢复。
            connection.execute("CREATE FUNCTION reject_native_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.event = 'UPDATE' THEN RAISE EXCEPTION 'history unavailable'; END IF; RETURN NEW; END $$")
            connection.execute('CREATE TRIGGER reject_native_history BEFORE INSERT ON mem0_history FOR EACH ROW EXECUTE FUNCTION reject_native_history()')
        answer = send(client, conversation, '我已经搬到苏州了')
        assert answer['automatic_memory']['status'] == 'failed'
        assert client.get('/api/memories').json()[0]['status'] == 'failed'
        with psycopg.connect(database_url) as connection:
            connection.execute('DROP TRIGGER reject_native_history ON mem0_history')
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        response = client.post(f"/api/conversations/{conversation}/messages/{answer['reply_to']}/memory/retry")
        assert response.status_code == 200, response.text
        entries = client.get('/api/memories').json()
        assert len(entries) == 1 and entries[0]['id'] == original['id']
        assert entries[0]['fact'] == '我住在苏州' and entries[0]['version'] == 2
        assert len(entries[0]['updates']) == 1 and entries[0]['updates'][0]['status'] == 'complete'


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


def test_native_budget_resumes_each_step_without_repaying(database_url: str, tmp_path: Path) -> None:
    """每次批准只放行一个云调用，多个原生步骤可恢复且不会循环重复提取。"""
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(native_provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        client.put('/api/settings/budget', json={'monthly_limit': '0'})
        assert client.post(f'/api/conversations/{conversation}/runs', json={'content': '我住在杭州'}).status_code == 202
        for _ in range(8):
            answer = wait_message(client, conversation, 'paused')
            pause = answer['budget_pause']
            assert client.post(f"/api/budget/operations/{pause['operation_id']}/approve", json={'pause_id': pause['pause_id']}).status_code == 200
            assert client.post(f"/api/conversations/{conversation}/messages/{answer['id']}/resume").status_code == 200
            current = wait_budget_step(client, conversation)
            if current['status'] == 'complete':
                break
        assert current['status'] == 'complete', current
        assert len(client.get('/api/memories').json()) == 1
        calls = client.get('/api/usage').json()
        assert len([call for call in calls if call['unit'] == 'chat_tokens']) == 3
        assert len([call for call in calls if call['unit'] == 'embedding_tokens']) == 3


def test_stopped_native_extraction_cannot_write_late(database_url: str, tmp_path: Path) -> None:
    """停止原生 LLM 等待后，迟到的推理与嵌入不得生成记忆。"""
    from threading import Event
    from time import sleep
    entered, release = Event(), Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """只阻塞原生推理 HTTP，不替换 SDK 内部逻辑。"""
        if json.loads(request.content).get('response_format'):
            entered.set()
            assert release.wait(5)
        return native_provider(request)
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        response = client.post(f'/api/conversations/{conversation}/runs', json={'content': '我住在杭州'})
        identifier = response.json()['messages'][-1]['id']
        assert entered.wait(5)
        try:
            assert client.post(f'/api/conversations/{conversation}/messages/{identifier}/stop').status_code == 200
        finally:
            release.set()
        sleep(.2)
        assert client.get('/api/memories').json() == []
        assert client.get(f'/api/conversations/{conversation}').json()['messages'][-1]['status'] == 'stopped'


def test_tool_update_resumes_budget_without_duplicate_mutation(database_url: str, tmp_path: Path) -> None:
    """工具规划、查找及更正逐步批准后仍只更新一次，已完成查询和模型回合不重复计费。"""
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(native_provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        send(client, conversation, '请记住：我住在杭州')
        count = len(client.get('/api/usage').json())
        client.put('/api/settings/budget', json={'monthly_limit': '0'})
        assert client.post(f'/api/conversations/{conversation}/runs', json={'content': '把我的居住地改成苏州'}).status_code == 202
        for _ in range(6):
            paused = wait_message(client, conversation, 'paused')
            pause = paused['budget_pause']
            assert client.post(f"/api/budget/operations/{pause['operation_id']}/approve", json={'pause_id': pause['pause_id']}).status_code == 200
            assert client.post(f"/api/conversations/{conversation}/messages/{paused['id']}/resume").status_code == 200
            current = wait_budget_step(client, conversation)
            if current['status'] == 'complete':
                break
        assert current['status'] == 'complete', current
        entries = client.get('/api/memories').json()
        assert len(entries) == 1 and entries[0]['version'] == 2 and entries[0]['fact'] == '我住在苏州'
        assert len(client.get('/api/usage').json()) - count == 4


def test_tool_cannot_mutate_unobserved_id_or_borrow_quoted_permission(database_url: str, tmp_path: Path) -> None:
    """模型猜测标识或把引文当成命令时，服务端拒绝实际删除。"""
    target = ''
    def provider(request: httpx.Request) -> httpx.Response:
        """模拟错误工具调用，后续接收到拒绝结果后停止。"""
        body = json.loads(request.content)
        if body.get('tools'):
            results = [json.loads(message['content']) for message in body['messages'] if message['role'] == 'tool']
            user = next(message['content'] for message in body['messages'] if message['role'] == 'user')
            if not results and user.startswith('朋友说'):
                return tool_reply('search_memories', {'query': ''})
            if results and 'memories' not in results[-1]:
                return json_reply('未删除记忆。')
            return tool_reply('delete_memory', {'memory_id': target, 'version': 1})
        return native_provider(request)
    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
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
