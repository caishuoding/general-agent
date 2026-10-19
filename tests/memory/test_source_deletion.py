"""经公开接口验证会话和附件来源删除的真实持久清理。"""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.auth.support import admin_client, authenticated_client
from tests.fake_mem0 import CallableTransport
from tests.memory.test_memory import configure_memory, memory_provider, configure_memory_settings
from tests.memory.test_memory_recall import recall_provider


def test_conversation_delete_rejects_old_preview_and_retained_memory_survives_restart(database_url: str, tmp_path: Path) -> None:
    """旧影响预览不扩大删除，整会话移除后保留事实可查可召回。"""
    transport = CallableTransport(recall_provider)
    configure_memory_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, transport) as client:
        conversation = configure_memory(client)
        client.post(f'/api/conversations/{conversation}/messages', json={'content': '请记住：我喜欢简洁的中文回答'})
        kept = client.get('/api/memories').json()[0]
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('原文件.txt', b'own source')}).json()[0]
        path = f'/api/conversations/{conversation}'
        preview = client.get(path + '/deletion-preview')
        assert preview.status_code == 200
        client.post(path + '/messages', json={'content': '请记住：我住在苏州'})
        stale = client.request('DELETE', path, json={'preview_revision': preview.json()['revision'], 'keep_memory_ids': [kept['id']]})
        assert stale.status_code == 409 and client.get(path).status_code == 200
        preview = client.get(path + '/deletion-preview').json()
        assert len(preview['memories']) == 2
        usage = len(client.get('/api/usage').json())
        decision = {'preview_revision': preview['revision'], 'keep_memory_ids': [kept['id']]}
        response = client.request('DELETE', path, json=decision)
        assert response.status_code == 200 and response.json()['status'] == 'complete'
        assert client.get(path).status_code == 404 and client.get('/api/conversations').json() == []
        assert not (tmp_path / 'attachments' / uploaded['id']).exists()
        entries = client.get('/api/memories').json()
        assert len(entries) == 1 and entries[0]['id'] == kept['id'] and entries[0]['status'] == 'complete'
        assert entries[0]['source_deleted'] is True and entries[0]['conversation_id'] is None and entries[0]['message_id'] is None
        assert client.request('DELETE', path, json=decision).json()['id'] == response.json()['id']
        assert len(client.get('/api/usage').json()) == usage


def test_attachment_delete_clears_search_links_and_original_file(database_url: str, tmp_path: Path) -> None:
    """删除文件清理原文件和搜索来源关联，保留已完成聊天正文。"""
    import json
    import psycopg
    from tests.attachments.test_attachments import configure
    from tests.search.test_search import reply, tool_call, tool_messages

    def provider(request: httpx.Request) -> httpx.Response:
        """模型先调用搜索工具，再根据真实结果回答。"""
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            return httpx.Response(200, json={'search_result': [{'link': 'https://service.example', 'title': '附近服务', 'content': '公开结果'}]})
        messages = tool_messages(payload)
        if messages:
            source = json.loads(messages[-1]['content'])['results'][0]
            return reply(payload, f'附近服务。[{source["key"]}]')
        if payload.get('tools'):
            return tool_call('search_web', {'query': '删除测试私密住址附近服务'})
        return reply(payload, '普通回答。')

    configure_memory_settings(database_url, tmp_path)
    with admin_client(database_url, tmp_path) as admin:
        assert admin.put(
            '/api/settings/search',
            json={'base_url': 'https://search.example', 'api_key': 'test-only'},
        ).status_code == 200

    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('私密.txt', '删除测试私密住址，主人陈敏。'.encode())}).json()[0]
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '联网搜索：根据资料查询服务'}).json()['messages'][-1]
        request = answer['search_action']
        path = f"/api/conversations/{conversation}/attachments/{uploaded['id']}"
        preview = client.get(path + '/deletion-preview').json()
        deleted = client.request('DELETE', path, json={'preview_revision': preview['revision']})
        assert deleted.status_code == 200
        message = next(item for item in client.get(f'/api/conversations/{conversation}').json()['messages'] if item['id'] == answer['id'])
        assert message['search_action']['status'] == 'expired' and message['search_action']['query'] == ''
        assert message['web_sources'] == []
        with psycopg.connect(database_url) as connection:
            assert connection.execute('SELECT count(*) FROM attachments WHERE id=%s', (uploaded['id'],)).fetchone() == (0,)
            assert connection.execute('SELECT query,results,sources FROM search_requests WHERE id=%s', (request['id'],)).fetchone() == ('', [], [])


def test_parser_late_result_cannot_restore_deleted_file(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """在真实文件读取边界暂停解析并制造 unlink 失败，晚到解析不能回填已停用来源。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import psycopg
    from tests.attachments.test_attachments import configure

    entered, release = Event(), Event()
    original_read = Path.read_bytes

    def blocked_read(path: Path) -> bytes:
        """只延迟本测试原附件的文件读取返回，不替换解析器或数据库模块。"""
        content = original_read(path)
        if path.parent == tmp_path / 'attachments' and len(path.name) == 36:
            entered.set()
            assert release.wait(10)
        return content

    monkeypatch.setattr(Path, 'read_bytes', blocked_read)
    configure_memory_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(memory_provider)) as client:
        conversation = configure(client)
        with ThreadPoolExecutor() as pool:
            upload = pool.submit(client.post, f'/api/conversations/{conversation}/attachments', files={'files': ('处理中.txt', b'parser private body')})
            try:
                assert entered.wait(5)
                file = client.get(f'/api/conversations/{conversation}/attachments').json()[0]
                path = f"/api/conversations/{conversation}/attachments/{file['id']}"
                physical = tmp_path / 'attachments' / file['id']
                physical.unlink()
                physical.mkdir()
                preview = client.get(path + '/deletion-preview').json()
                failed = client.request('DELETE', path, json={'preview_revision': preview['revision']})
                assert failed.status_code == 503 and failed.json()['phase'] == 'files'
            finally:
                release.set()
            assert upload.result(timeout=5).status_code == 201
        assert client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status'] == 'deleting'
        with psycopg.connect(database_url) as connection:
            assert connection.execute('SELECT content FROM attachments WHERE id=%s', (file['id'],)).fetchone() == (None,)
        physical.rmdir()
        assert client.post('/api/source-deletions/' + failed.json()['id'] + '/retry').json()['status'] == 'complete'
        with psycopg.connect(database_url) as connection:
            assert connection.execute('SELECT count(*) FROM attachments WHERE id=%s', (file['id'],)).fetchone() == (0,)
