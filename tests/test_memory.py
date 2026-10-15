"""通过真实 Mem0 和 PostgreSQL 验证聊天明确保存及来源。"""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_streaming import wait_message


def memory_provider(request: httpx.Request) -> httpx.Response:
    """只替换云端 HTTP，嵌入提供确定向量，普通回答不负责写记忆。"""
    payload = json.loads(request.content)
    if request.url.path.endswith('/embeddings'):
        return httpx.Response(200, json={"model": "embed", "data": [{"index": index, "embedding": [1.0, 0.0, 0.0]} for index, _ in enumerate(payload['input'])], "usage": {"prompt_tokens": 7, "total_tokens": 7}})
    if payload.get('stream'):
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"普通回答"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    return httpx.Response(200, json={"choices": [{"message": {"content": "普通回答"}}]})


def configure_memory(client: TestClient) -> str:
    """配置受控聊天与已验证嵌入，并创建一个独立会话。"""
    client.put('/api/settings', json={"base_url": "https://chat.example/v1", "model": "chat", "api_key": "test-key"})
    client.put('/api/settings/embedding', json={"base_url": "https://vector.example/v1", "model": "embed", "api_key": "test-key", "dimensions": 3})
    assert client.post('/api/settings/capabilities/embedding').json()['status'] == 'passed'
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
        assert '已保存' in answer['content']
        entries = client.get('/api/memories').json()
        assert len(entries) == 1
        assert entries[0]['fact'] == '我喜欢简洁的中文回答'
        assert entries[0]['status'] == 'complete'
        assert entries[0]['conversation_id'] == conversation_id
        source = client.get(f'/api/conversations/{conversation_id}').json()['messages'][0]
        assert entries[0]['message_id'] == source['id']
        assert answer['memory_action']['memory_id'] == entries[0]['memory_id']
        assert client.post(endpoint, json={"content": "请记住：我喜欢简洁的中文回答"}, headers={'X-Operation-ID': operation}).status_code == 202
        assert len(client.get('/api/memories').json()) == 1
        calls = client.get('/api/usage').json()
        assert len(calls) == 2
        assert all(call['unit'] == 'embedding_tokens' for call in calls)
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as restarted:
        assert restarted.get('/api/memories').json() == entries
        assert restarted.get(f'/api/conversations/{conversation_id}').json()['messages'][-1]['status'] == 'complete'


def test_saved_fact_survives_worker_budget_pause_and_resume(database_url: str, tmp_path: Path) -> None:
    """保存嵌入完成后下一次回答请求独立暂停；刷新后批准仅继续回答，不重新保存。"""
    transport = httpx.MockTransport(memory_provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        client.put('/api/settings/budget', json={"monthly_limit": "0.000014", "prices": [{"base_url": "https://vector.example/v1", "model": "embed", "input_per_million": "1"}, {"base_url": "https://chat.example/v1", "model": "chat", "input_per_million": "1", "output_per_million": "1"}]})
        response = client.post(f'/api/conversations/{conversation_id}/runs', json={"content": "请记住：我喜欢茶；再给我一个泡茶建议"})
        assert response.status_code == 202
        paused = wait_message(client, conversation_id, 'paused')
        assert paused['memory_action']['status'] == 'complete'
        assert len(client.get('/api/memories').json()) == 1
        assert len(client.get('/api/usage').json()) == 2
        pause = paused['budget_pause']
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as restarted:
        assert wait_message(restarted, conversation_id, 'paused')['budget_pause'] == pause
        assert restarted.post(f"/api/budget/operations/{pause['operation_id']}/approve", json={'pause_id': pause['pause_id']}).status_code == 200
        path = f"/api/conversations/{conversation_id}/messages/{paused['id']}/resume"
        assert restarted.post(path).status_code == 200
        complete = wait_message(restarted, conversation_id, 'complete')
        assert complete['content'] == '普通回答'
        assert complete['memory_action']['memory_id'] == paused['memory_action']['memory_id']
        assert len(restarted.get('/api/memories').json()) == 1
        assert len(restarted.get('/api/usage').json()) == 3


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
        assert [entry['fact'] for entry in client.get('/api/memories').json()] == ['我喜欢中文回答']


def test_partial_mem0_history_failure_reconciles_without_duplicate_or_second_embedding(database_url: str, tmp_path: Path) -> None:
    """真实数据库拒绝历史写入时不假报完成；恢复先核对已提交向量，再修复历史。"""
    import psycopg
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        # 在数据库边界安排持久化故障，不替换 Mem0 或应用内部模块。
        with psycopg.connect(database_url) as connection:
            connection.execute("CREATE FUNCTION reject_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'history unavailable'; END $$")
            connection.execute('CREATE TRIGGER reject_history BEFORE INSERT ON mem0_history FOR EACH ROW EXECUTE FUNCTION reject_history()')
        assert client.post(f'/api/conversations/{conversation_id}/runs',json={'content':'请记住：我喜欢茶'}).status_code == 202
        failed = wait_message(client,conversation_id,'failed')
        assert failed['memory_action']['status'] == 'failed'
        assert '已保存' not in failed['content']
        with psycopg.connect(database_url) as connection:
            connection.execute('DROP TRIGGER reject_history ON mem0_history')
        response = client.post(f"/api/conversations/{conversation_id}/messages/{failed['id']}/retry")
        assert response.status_code == 202
        complete = wait_message(client,conversation_id,'complete')
        assert complete['memory_action']['status'] == 'complete'
        entries = client.get('/api/memories').json()
        assert len(entries) == 1 and entries[0]['fact'] == '我喜欢茶'
        assert len(client.get('/api/usage').json()) == 2


def test_stop_during_embedding_does_not_write_after_response(database_url: str, tmp_path: Path) -> None:
    """用户停止等待中的嵌入后，已发生用量保留，但尚未开始的记忆写入不继续。"""
    from threading import Event
    from time import monotonic, sleep
    entered,release = Event(),Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """只在保存事实的外部嵌入等待，能力验证正常完成。"""
        if '我喜欢茶' in request.content.decode():
            entered.set()
            assert release.wait(5)
        return memory_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
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
        usage = client.get('/api/usage').json()
        assert len(usage) == 2 and all(item['prompt_tokens'] == 7 for item in usage)


def test_quoted_nickname_is_explicit_but_fiction_is_not_personal_memory(database_url: str, tmp_path: Path) -> None:
    """事实中的普通引号不妨碍直接保存；写作素材中的命令词不取得个人记忆权限。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        assert client.post(f'/api/conversations/{conversation_id}/messages',json={'content':'请记住：我的昵称是"小林"'}).status_code == 200
        assert [entry['fact'] for entry in client.get('/api/memories').json()] == ['我的昵称是"小林"']
        response = client.post(f'/api/conversations/{conversation_id}/messages',json={'content':'写一段故事；记住：角色小明住在杭州'})
        assert response.status_code == 200
        assert len(client.get('/api/memories').json()) == 1


def test_embedding_budget_approval_cannot_move_to_changed_configuration(database_url: str, tmp_path: Path) -> None:
    """嵌入调用前的预算批准绑定原嵌入配置，不因当前聊天模型没变而被挪用。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
        conversation_id = configure_memory(client)
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        operation = str(uuid4())
        path = f'/api/conversations/{conversation_id}/runs'
        body = {'content':'请记住：我喜欢茶'}
        paused = client.post(path,json=body,headers={'X-Operation-ID':operation})
        assert paused.status_code == 402
        assert client.get(f'/api/conversations/{conversation_id}').json()['messages'] == []
        assert client.post(f'/api/budget/operations/{operation}/approve',json={'pause_id':paused.json()['pause_id']}).status_code == 200
        client.put('/api/settings/embedding',json={'base_url':'https://changed.example/v1','model':'embed','api_key':'test-key','dimensions':3})
        replayed = client.post(path,json=body,headers={'X-Operation-ID':operation})
        assert replayed.status_code == 409
        assert client.get('/api/memories').json() == []


def test_answer_failure_retry_preserves_saved_memory_and_counts_only_new_answer(database_url: str, tmp_path: Path) -> None:
    """记忆已保存但回答失败时，重试复用原事实与来源，只计量新的回答调用。"""
    fail_answer = True
    def provider(request: httpx.Request) -> httpx.Response:
        """第一次回答失败而嵌入成功，后续回答恢复正常。"""
        nonlocal fail_answer
        if request.url.path.endswith('/chat/completions') and fail_answer:
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
        assert len([call for call in calls if call['unit'] == 'embedding_tokens']) == 2
        assert len([call for call in calls if call['unit'] == 'chat_tokens']) == 2


def test_missing_embedding_is_real_failure_then_retry_uses_same_source(database_url: str, tmp_path: Path) -> None:
    """未配置嵌入时不会声称记住；配置补齐后重试同一来源完成正式保存。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
        client.put('/api/settings',json={'base_url':'https://chat.example/v1','model':'chat','api_key':'test'})
        conversation_id = str(client.post('/api/conversations',json={}).json()['id'])
        client.post(f'/api/conversations/{conversation_id}/runs',json={'content':'请记住：我喜欢茶'})
        failed = wait_message(client,conversation_id,'failed')
        assert '嵌入' in failed['error']
        assert client.get('/api/memories').json()[0]['status'] == 'failed'
        assert client.get('/api/usage').json() == []
        configure_memory(client)
        assert client.post(f"/api/conversations/{conversation_id}/messages/{failed['id']}/retry").status_code == 202
        complete = wait_message(client,conversation_id,'complete')
        assert complete['reply_to'] == failed['reply_to']
        assert len(client.get('/api/memories').json()) == 1
