"""从公开接口验证忘记记忆、原文保留与旧授权失效，正式存储使用真实 Mem0。"""

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.memory.test_memory import configure_memory
from tests.memory.test_memory_recall import recall_provider


def test_delete_memory_preserves_chat_and_other_entries_without_cloud_calls(database_url: str, tmp_path: Path) -> None:
    """管理页删除只忘记指定条目且不发云端请求，原聊天及其他记忆跨重启保留。"""
    transport = httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        source = configure_memory(client)
        for fact in ['我喜欢简洁的中文回答', '我住在苏州']:
            assert client.post(f'/api/conversations/{source}/messages', json={'content': '请记住：' + fact}).status_code == 200
        entries = client.get('/api/memories').json()
        target = next(row for row in entries if row['fact'] == '我喜欢简洁的中文回答')
        other = next(row for row in entries if row['fact'] == '我住在苏州')
        count = len(client.get('/api/usage').json())
        response = client.delete(f"/api/memories/{target['id']}")
        assert response.status_code == 200
        assert response.json()['status'] == 'complete'
        assert client.delete(f"/api/memories/{target['id']}").json()['status'] == 'complete'
        assert len(client.get('/api/usage').json()) == count
        remaining = client.get('/api/memories').json()
        assert len(remaining) == 1 and remaining[0]['id'] == other['id']
        original = client.get(f'/api/conversations/{source}').json()
        assert original['messages'][0]['content'] == '请记住：我喜欢简洁的中文回答'
        assert original['messages'][1]['memory_action']['status'] == 'deleted'
        assert original['messages'][1]['memory_action']['fact'] == ''
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        assert client.get('/api/memories').json()[0]['id'] == other['id']
        conversation = client.post('/api/conversations', json={}).json()['id']
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '按我的偏好回答'})
        assert '我喜欢简洁的中文回答' not in response.json()['messages'][-1]['content']
        assert client.delete(f"/api/memories/{target['id']}").json()['status'] == 'complete'
        assert len(client.get('/api/memories').json()) == 1


def test_explicit_chat_delete_is_local_and_ambiguous_request_only_clarifies(database_url: str, tmp_path: Path) -> None:
    """明确完整事实可从聊天删除；含糊或混合写作指令只澄清且不触发云端调用。"""
    from tests.conversation.test_streaming import wait_message

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(recall_provider)), base_url='http://127.0.0.1:8000') as client:
        source = configure_memory(client)
        for fact in ['我喜欢简洁的中文回答', '我住在苏州']:
            client.post(f'/api/conversations/{source}/messages', json={'content': '请记住：' + fact})
        clarification = client.post(f'/api/conversations/{source}/messages', json={'content': '忘记记忆：那个事情'})
        assert clarification.status_code == 200
        assert '未删除' in clarification.json()['messages'][-1]['content']
        assert len(client.get('/api/memories').json()) == 2
        response = client.post(f'/api/conversations/{source}/messages', json={'content': '删除记忆：我喜欢简洁的中文回答'})
        assert response.status_code == 200
        assert len(client.get('/api/memories').json()) == 1
        entry = client.get('/api/memories').json()[0]
        started = client.post(f'/api/conversations/{source}/runs', json={'content': f"忘记记忆 {entry['id']}"})
        assert started.status_code == 202
        finished = wait_message(client, source, 'complete')
        assert finished['status'] == 'complete'
        assert client.get('/api/memories').json() == []


def test_delete_during_inflight_update_cannot_be_overwritten_by_late_result(database_url: str, tmp_path: Path) -> None:
    """更正嵌入仍等待时删除立即停用；迟到响应不会覆盖墓碑或使 SDK 正文留存。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep
    from tests.conversation.test_streaming import wait_message
    from tests.fake_mem0 import FakeMem0

    entered, release = Event(), Event()
    memories = FakeMem0()

    def mem0_provider(request: httpx.Request) -> httpx.Response:
        """阻塞正式更正写入，同时让删除先完成停用。"""
        if request.method == 'PUT' and request.url.path.startswith('/memories/'):
            entered.set()
            assert release.wait(10)
        return memories.handle(request)

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(recall_provider), httpx.MockTransport(mem0_provider)), base_url='http://127.0.0.1:8000') as client:
        source = configure_memory(client)
        client.post(f'/api/conversations/{source}/messages', json={'content': '请记住：我住在杭州'})
        entry = client.get('/api/memories').json()[0]
        client.post(f'/api/conversations/{source}/runs', json={'content': '更正记忆：我住在杭州 => 我住在苏州'})
        assert entered.wait(5)
        with ThreadPoolExecutor() as pool:
            deletion = pool.submit(client.delete, f"/api/memories/{entry['id']}")
            try:
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    row = client.get('/api/memories').json()[0]
                    if row['status'] == 'deleting':
                        break
                    sleep(.02)
                assert row['status'] == 'deleting' and row['fact'] == ''
                assert not deletion.done()
            finally:
                release.set()
            assert deletion.result(timeout=10).json()['status'] == 'complete'
        stopped = wait_message(client, source, 'stopped')
        assert stopped['memory_update']['status'] == 'deleted'
        assert client.get('/api/memories').json() == []
