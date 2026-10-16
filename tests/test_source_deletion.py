"""经公开接口验证会话和附件来源删除、关联记忆选择及真实持久清理。"""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_attachment_memory import attachment_provider
from test_memory import configure_memory


def test_attachment_preview_defaults_to_delete_derived_memories_but_preserves_independent_upload(database_url: str, tmp_path: Path) -> None:
    """单文件影响预览只含其批准事实，默认清原文件和正式记忆，相同内容另一上传保持可用。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(attachment_provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure_memory(client)
        content='陈敏住在杭州。陈敏喜欢乌龙茶。'.encode()
        file=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('朋友.txt',content)}).json()[0]
        answer=client.post(f'/api/conversations/{conversation}/messages',json={'content':'请从附件提取记忆','attachment_ids':[file['id']]}).json()['messages'][-1]
        candidate=answer['memory_candidates'][0]
        client.put(f"/api/memory-candidates/{candidate['id']}/subject",json={'kind':'other','label':'陈敏'})
        assert client.post(f"/api/memory-candidates/{candidate['id']}/confirm").status_code==200
        memory=client.get('/api/memories').json()[0]
        other=client.post('/api/conversations',json={}).json()['id']
        independent=client.post(f'/api/conversations/{other}/attachments',files={'files':('朋友.txt',content)}).json()[0]
        path=f"/api/conversations/{conversation}/attachments/{file['id']}"
        preview=client.get(path+'/deletion-preview')
        assert preview.status_code==200
        assert [entry['id'] for entry in preview.json()['memories']]==[memory['id']]
        assert preview.json()['memories'][0]['delete_by_default'] is True
        usage=len(client.get('/api/usage').json())
        result=client.request('DELETE',path,json={'preview_revision':preview.json()['revision']})
        assert result.status_code==200 and result.json()['status']=='complete'
        assert client.get(path+'/file').status_code==404
        assert not (tmp_path/'attachments'/file['id']).exists()
        assert client.get('/api/memories').json()==[]
        assert client.get(f"/api/conversations/{other}/attachments/{independent['id']}/file").content==content
        assert client.get(f'/api/conversations/{conversation}').json()['messages'][0]['content']=='请从附件提取记忆'
        assert client.post(f"/api/memory-candidates/{candidate['id']}/confirm").status_code in (404,409)
        assert len(client.get('/api/usage').json())==usage
        assert client.get('/api/source-deletions/'+result.json()['id']).json()['status']=='complete'
        assert client.request('DELETE',path,json={'preview_revision':preview.json()['revision']}).json()['id']==result.json()['id']


def test_conversation_delete_rejects_old_preview_and_retained_memory_survives_restart_and_can_be_forgotten(database_url: str, tmp_path: Path) -> None:
    """旧影响预览不扩大删除，整会话移除后保留事实可查可召回且以后仍能正常删除。"""
    transport=httpx.MockTransport(attachment_provider)
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


def test_failed_source_cleanup_is_inaccessible_and_resumes_exact_keep_selection_after_restart(database_url: str, tmp_path: Path) -> None:
    """真实 SDK 历史清理失败后原文件仍在但不可读，刷新与重启可沿原批准继续，不重复收费。"""
    import psycopg
    transport=httpx.MockTransport(attachment_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        conversation=configure_memory(client)
        file=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('保留.txt','陈敏住在杭州。陈敏喜欢乌龙茶。'.encode())}).json()[0]
        answer=client.post(f'/api/conversations/{conversation}/messages',json={'content':'请从附件提取记忆','attachment_ids':[file['id']]}).json()['messages'][-1]
        for candidate in answer['memory_candidates']:
            client.put(f"/api/memory-candidates/{candidate['id']}/subject",json={'kind':'other','label':'陈敏'})
            client.post(f"/api/memory-candidates/{candidate['id']}/confirm")
        kept=client.get('/api/memories').json()[0]
        path=f"/api/conversations/{conversation}/attachments/{file['id']}"
        preview=client.get(path+'/deletion-preview').json()
        with psycopg.connect(database_url) as connection:
            # 模拟来源删除时 SDK 历史清理失败，验证原文件停用和精确恢复。
            connection.execute("CREATE FUNCTION reject_source_purge() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture history failure'; END $$")
            connection.execute('CREATE TRIGGER reject_source_purge BEFORE DELETE ON mem0_history FOR EACH ROW EXECUTE FUNCTION reject_source_purge()')
        count=len(client.get('/api/usage').json())
        failed=client.request('DELETE',path,json={'preview_revision':preview['revision'],'keep_memory_ids':[kept['id']]})
        assert failed.status_code==503 and failed.json()['status']=='failed'
        assert (tmp_path/'attachments'/file['id']).is_file()
        assert client.get(path+'/file').status_code==404
        assert client.post(f'/api/conversations/{conversation}/messages',json={'content':'读取资料','attachment_ids':[file['id']]}).status_code==409
        assert client.get('/api/source-deletions/'+failed.json()['id']).json()['phase']=='memories'
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
    with psycopg.connect(database_url) as connection:
        connection.execute('DROP TRIGGER reject_source_purge ON mem0_history')
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        result=client.post('/api/source-deletions/'+failed.json()['id']+'/retry')
        assert result.status_code==200 and result.json()['status']=='complete'
        assert client.get(path+'/file').status_code==404 and not (tmp_path/'attachments'/file['id']).exists()
        entries=client.get('/api/memories').json()
        assert len(entries)==1 and entries[0]['id']==kept['id'] and entries[0]['source_deleted'] and entries[0]['status']=='complete'
        assert len(client.get('/api/usage').json())==count


def test_attachment_delete_clears_summary_steps_search_drafts_and_old_approvals(database_url: str, tmp_path: Path) -> None:
    """删除文件主动清全文中间正文和搜索查询许可，保留原已完成聊天而不继续提供有效文件出处。"""
    import json
    import psycopg
    from test_attachments import configure
    from test_search import reply
    from test_summary import summary_response

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
    from test_memory import memory_provider
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(memory_provider)),base_url='http://127.0.0.1:8000') as client:
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


def test_deleting_during_attachment_candidate_embedding_cannot_publish_late_fact(database_url: str, tmp_path: Path) -> None:
    """真实嵌入请求晚到时，来源先停用再等待保存锁，最终无正式或派生正文残留。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    import psycopg
    entered,release=Event(),Event()
    block=False

    def provider(request: httpx.Request) -> httpx.Response:
        """只阻塞已经批准事实的外部嵌入返回，初始化能力检查照常完成。"""
        if block and request.url.path.endswith('/embeddings'):
            entered.set()
            assert release.wait(15)
        return attachment_provider(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure_memory(client)
        file=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('朋友.txt','陈敏住在杭州。陈敏喜欢乌龙茶。'.encode())}).json()[0]
        answer=client.post(f'/api/conversations/{conversation}/messages',json={'content':'请从附件提取记忆','attachment_ids':[file['id']]}).json()['messages'][-1]
        candidate=answer['memory_candidates'][0]
        client.put(f"/api/memory-candidates/{candidate['id']}/subject",json={'kind':'other','label':'陈敏'})
        block=True
        with ThreadPoolExecutor() as pool:
            saving=pool.submit(client.post,f"/api/memory-candidates/{candidate['id']}/confirm")
            try:
                assert entered.wait(5)
                path=f"/api/conversations/{conversation}/attachments/{file['id']}"
                preview=client.get(path+'/deletion-preview').json()
                assert preview['memories'] and not preview['memories'][0]['can_keep']
                deleting=pool.submit(client.request,'DELETE',path,json={'preview_revision':preview['revision']})
                for _ in range(100):
                    if client.get(path+'/file').status_code==404:
                        break
                    time.sleep(.02)
                assert client.get(path+'/file').status_code==404
            finally:
                release.set()
            assert saving.result(timeout=10).status_code in (409,502)
            assert deleting.result(timeout=10).status_code==200
        assert client.get('/api/memories').json()==[]
        with psycopg.connect(database_url) as connection:
            assert connection.execute('SELECT count(*) FROM mem0_history').fetchone()==(0,)
            assert connection.execute("SELECT count(*) FROM memory_candidates WHERE fact<>'' OR source_quote<>''").fetchone()==(0,)


def test_parser_late_result_cannot_restore_chunks_during_failed_file_cleanup(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """在真实文件读取边界暂停解析并制造 unlink 失败，晚到解析不能回填已停用来源。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    import psycopg
    from test_attachments import configure
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
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(attachment_provider)),base_url='http://127.0.0.1:8000') as client:
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


def test_unapproved_change_candidate_does_not_make_other_conversation_memory_a_source(database_url: str, tmp_path: Path) -> None:
    """升级前未批准或已拒绝的变更不构成正式来源，删除其会话不得误删旧记忆。"""
    import json
    from test_memory_recall import recall_provider
    from test_memory_candidates import seed_legacy_candidate

    def provider(request: httpx.Request) -> httpx.Response:
        """将当前用户变化提为候选，保持正式目标来自服务端白名单。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
            context=json.loads(payload['messages'][1]['content'])
            item={'fact':'我住在杭州','quote':'最近在杭州','subject':'user','target_id':context['targets'][0]['id']}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'candidates':[item]},ensure_ascii=False)}}]})
        return recall_provider(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        original=configure_memory(client)
        client.post(f'/api/conversations/{original}/messages',json={'content':'请记住：我住在苏州'})
        kept=client.get('/api/memories').json()[0]
        for reject in (False,True):
            other=client.post('/api/conversations',json={}).json()['id']
            answer=client.post(f'/api/conversations/{other}/messages',json={'content':'最近在杭州'}).json()['messages'][-1]
            candidate=seed_legacy_candidate(database_url,other,answer['reply_to'],'最近在杭州',kept)
            assert candidate['target_id']==kept['id']
            if reject:
                client.post(f"/api/memory-candidates/{candidate['id']}/reject")
            preview=client.get(f'/api/conversations/{other}/deletion-preview').json()
            assert preview['memories']==[]
            assert client.request('DELETE',f'/api/conversations/{other}',json={'preview_revision':preview['revision']}).status_code==200
            entry=client.get('/api/memories').json()[0]
            assert entry['id']==kept['id'] and entry['fact']=='我住在苏州' and not entry['source_deleted']


def test_deleted_summary_source_cannot_be_repopulated_or_retry_fall_back_to_other_files(database_url: str, tmp_path: Path) -> None:
    """文件在全文云调用期间删除后不缓存晚到正文，旧请求重试不能偷偷转用其他文件。"""
    from threading import Event
    import json
    import psycopg
    from test_attachments import configure
    from test_summary import summary_response
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
