"""经公开接口验证会话和附件来源删除、关联记忆选择及真实持久清理。"""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.fake_mem0 import CallableTransport
from tests.memory.test_memory import configure_memory, memory_provider
from tests.memory.test_memory_recall import recall_provider


def test_conversation_delete_rejects_old_preview_and_retained_memory_survives_restart_and_can_be_forgotten(database_url: str, tmp_path: Path) -> None:
    """旧影响预览不扩大删除，整会话移除后保留事实可查可召回且以后仍能正常删除。"""
    transport=CallableTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        conversation=configure_memory(client)
        client.post(f'/api/conversations/{conversation}/messages',json={'content':'请记住：我喜欢简洁的中文回答'})
        kept=client.get('/api/memories').json()[0]
        file=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('原文件.txt',b'own source')}).json()[0]
        path=f'/api/conversations/{conversation}'
        preview=client.get(path+'/deletion-preview')
        assert preview.status_code==200
        client.post(path+'/messages',json={'content':'请记住：我住在苏州'})
        stale=client.request('DELETE',path,json={'preview_revision':preview.json()['revision'],'keep_memory_ids':[kept['id']]})
        assert stale.status_code==409 and client.get(path).status_code==200
        preview=client.get(path+'/deletion-preview').json()
        assert len(preview['memories'])==2
        usage=len(client.get('/api/usage').json())
        decision={'preview_revision':preview['revision'],'keep_memory_ids':[kept['id']]}
        response=client.request('DELETE',path,json=decision)
        assert response.status_code==200 and response.json()['status']=='complete'
        assert client.get(path).status_code==404 and client.get('/api/conversations').json()==[]
        assert not (tmp_path/'attachments'/file['id']).exists()
        entries=client.get('/api/memories').json()
        assert len(entries)==1 and entries[0]['id']==kept['id'] and entries[0]['status']=='complete'
        assert entries[0]['source_deleted'] is True and entries[0]['conversation_id'] is None and entries[0]['message_id'] is None
        assert client.request('DELETE',path,json=decision).json()['id']==response.json()['id']
        assert len(client.get('/api/usage').json())==usage
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        new=client.post('/api/conversations',json={}).json()['id']
        answer=client.post(f'/api/conversations/{new}/messages',json={'content':'按我的偏好回答'}).json()['messages'][-1]
        assert '简洁的中文回答' in answer['content']
        assert client.delete('/api/memories/'+kept['id']).status_code==200
        assert client.get('/api/memories').json()==[]


def test_attachment_delete_clears_summary_steps_search_drafts_and_old_approvals(database_url: str, tmp_path: Path) -> None:
    """删除文件主动清全文中间正文和搜索查询许可，保留原已完成聊天而不继续提供有效文件出处。"""
    import json
    import psycopg
    from tests.attachments.test_attachments import configure
    from tests.search.test_search import reply
    from tests.conversation.test_summary import summary_response

    def provider(request: httpx.Request) -> httpx.Response:
        """模型规划只返回可观察的私密查询，其余按实际资料标记回答。"""
        payload=json.loads(request.content)
        if payload['messages'][0]['content'].startswith('为明确联网请求生成查询'):
            return reply(payload,json.dumps({'query':'删除测试私密住址附近服务'},ensure_ascii=False))
        return summary_response(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test-only'})
        file=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('私密.txt','删除测试私密住址，主人陈敏。'.encode())}).json()[0]
        summary=client.post(f'/api/conversations/{conversation}/messages',json={'content':'请全文总结资料','attachment_ids':[file['id']]}).json()['messages'][-1]
        assert summary['document_progress']['status']=='complete' and summary['citations']
        search=client.post(f'/api/conversations/{conversation}/messages',json={'content':'联网搜索：根据附件查询服务'}).json()['messages'][-1]
        request=search['search_action']
        assert request['status']=='pending' and search['status']=='paused'
        path=f"/api/conversations/{conversation}/attachments/{file['id']}"
        preview=client.get(path+'/deletion-preview').json()
        deleted=client.request('DELETE',path,json={'preview_revision':preview['revision']})
        assert deleted.status_code==200
        rows=client.get(f'/api/conversations/{conversation}').json()['messages']
        saved_summary=next(item for item in rows if item['id']==summary['id'])
        assert saved_summary['content']==summary['content'] and saved_summary['citations']==[] and saved_summary['document_progress'] is None
        saved_search=next(item for item in rows if item['id']==search['id'])
        assert saved_search['search_action']['status']=='expired' and saved_search['search_action']['query']==''
        assert saved_search['status']=='stopped'
        decision=client.post(f"/api/conversations/{conversation}/search/{request['id']}/decision",json={'revision':request['revision'],'decision':'approve'})
        assert decision.status_code==409
        # 持久正文清理契约：公开界面缺少来源不足以证明后台分段、查询和解析正文实际消失。
        with psycopg.connect(database_url) as connection:
            assert connection.execute('SELECT count(*) FROM document_jobs').fetchone()==(0,)
            assert connection.execute('SELECT count(*) FROM document_steps').fetchone()==(0,)
            assert connection.execute('SELECT count(*) FROM attachment_chunks').fetchone()==(0,)
            assert connection.execute("SELECT query,results,sources FROM search_requests WHERE id=%s",(request['id'],)).fetchone()==('',[],[])


def test_conversation_delete_clears_cross_conversation_history_and_correction_source(database_url: str, tmp_path: Path) -> None:
    """更正来源会话关联原记忆，删除后旧历史结果失效，其他会话完成正文仍保留。"""
    from tests.memory.test_memory import memory_provider
    with TestClient(create_app(database_url,tmp_path,CallableTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
        original=configure_memory(client)
        client.post(f'/api/conversations/{original}/messages',json={'content':'请记住：我喜欢红茶'})
        entry=client.get('/api/memories').json()[0]
        correction=client.post('/api/conversations',json={}).json()['id']
        updated=client.post(f'/api/conversations/{correction}/messages',json={'content':'更正记忆：我喜欢红茶 => 我喜欢白茶'})
        assert updated.status_code==200
        other=client.post('/api/conversations',json={}).json()['id']
        history=client.post(f'/api/conversations/{other}/messages',json={'content':'查找旧聊天：白茶'}).json()['messages'][-1]
        assert history['history_sources']
        path=f'/api/conversations/{correction}'
        preview=client.get(path+'/deletion-preview').json()
        assert [row['id'] for row in preview['memories']]==[entry['id']]
        response=client.request('DELETE',path,json={'preview_revision':preview['revision'],'keep_memory_ids':[entry['id']]})
        assert response.status_code==200, response.text
        kept=client.get('/api/memories').json()[0]
        assert kept['fact']=='我喜欢白茶' and kept['source_deleted']
        assert kept['updates'][0]['source_deleted'] and kept['updates'][0]['fact']==''
        assert client.get(f'/api/conversations/{original}').status_code==200
        after=client.get(f'/api/conversations/{other}').json()['messages'][-1]
        assert after['history_search']['status']=='unavailable' and after['history_sources']==[]


def test_parser_late_result_cannot_restore_chunks_during_failed_file_cleanup(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """在真实文件读取边界暂停解析并制造 unlink 失败，晚到解析不能回填已停用来源。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    import psycopg
    from tests.attachments.test_attachments import configure
    entered,release=Event(),Event()
    original_read=Path.read_bytes

    def blocked_read(path: Path) -> bytes:
        """只延迟本测试原附件的文件读取返回，不替换解析器或数据库模块。"""
        content=original_read(path)
        if path.parent==tmp_path/'attachments' and len(path.name)==36:
            entered.set()
            assert release.wait(10)
        return content

    monkeypatch.setattr(Path,'read_bytes',blocked_read)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        with ThreadPoolExecutor() as pool:
            upload=pool.submit(client.post,f'/api/conversations/{conversation}/attachments',files={'files':('处理中.txt',b'parser private body')})
            try:
                assert entered.wait(5)
                file=client.get(f'/api/conversations/{conversation}/attachments').json()[0]
                path=f"/api/conversations/{conversation}/attachments/{file['id']}"
                physical=tmp_path/'attachments'/file['id']
                physical.unlink()
                physical.mkdir()
                preview=client.get(path+'/deletion-preview').json()
                failed=client.request('DELETE',path,json={'preview_revision':preview['revision']})
                assert failed.status_code==503 and failed.json()['phase']=='files'
            finally:
                release.set()
            assert upload.result(timeout=5).status_code==201
        assert client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status']=='deleting'
        with psycopg.connect(database_url) as connection:
            assert connection.execute('SELECT count(*) FROM attachment_chunks').fetchone()==(0,)
        physical.rmdir()
        assert client.post('/api/source-deletions/'+failed.json()['id']+'/retry').json()['status']=='complete'


def test_deleted_summary_source_cannot_be_repopulated_or_retry_fall_back_to_other_files(database_url: str, tmp_path: Path) -> None:
    """文件在全文云调用期间删除后不缓存晚到正文，旧请求重试不能偷偷转用其他文件。"""
    from threading import Event
    import json
    import psycopg
    from tests.attachments.test_attachments import configure
    from tests.conversation.test_summary import summary_response
    entered,release=Event(),Event()

    def provider(request: httpx.Request) -> httpx.Response:
        """阻塞实际分段请求，删除完成后才返回仍包含原文件依据的回答。"""
        payload=json.loads(request.content)
        if payload['messages'][0]['content'].startswith('对以下资料分段'):
            entered.set()
            assert release.wait(10)
        return summary_response(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        old=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('其他.txt',b'other document')}).json()[0]
        client.post(f'/api/conversations/{conversation}/messages',json={'content':'收到这个文件','attachment_ids':[old['id']]})
        file=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('当前.txt',b'current secret body')}).json()[0]
        try:
            run=client.post(f'/api/conversations/{conversation}/runs',json={'content':'请全文总结资料','attachment_ids':[file['id']]}).json()['messages'][-1]
            assert entered.wait(5)
            path=f"/api/conversations/{conversation}/attachments/{file['id']}"
            preview=client.get(path+'/deletion-preview').json()
            assert client.request('DELETE',path,json={'preview_revision':preview['revision']}).status_code==200
        finally:
            release.set()
        assert client.post(f"/api/conversations/{conversation}/messages/{run['id']}/retry").status_code==409
    # 应用关闭等待工作线程收尾后，持久正文也不能重新出现。
    with psycopg.connect(database_url) as connection:
        assert connection.execute('SELECT count(*) FROM document_jobs').fetchone()==(0,)
        assert connection.execute('SELECT count(*) FROM document_steps').fetchone()==(0,)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        message=client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
        assert message['status']=='stopped' and message['content']=='' and message['citations']==[]
