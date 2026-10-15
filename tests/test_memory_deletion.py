"""从公开接口验证忘记记忆、原文保留与旧授权失效，正式存储使用真实 Mem0。"""

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_memory import configure_memory
from test_memory_recall import recall_provider


def test_delete_memory_preserves_chat_and_other_entries_without_cloud_calls(database_url: str, tmp_path: Path) -> None:
    """管理页删除只忘记指定条目，零预算仍可本地清理，原聊天及其他记忆跨重启保留。"""
    transport=httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        for fact in ['我喜欢简洁的中文回答','我住在苏州']:
            assert client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：'+fact}).status_code==200
        entries=client.get('/api/memories').json()
        target=next(row for row in entries if row['fact']=='我喜欢简洁的中文回答')
        other=next(row for row in entries if row['fact']=='我住在苏州')
        count=len(client.get('/api/usage').json())
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        response=client.delete(f"/api/memories/{target['id']}")
        assert response.status_code==200
        assert response.json()['status']=='complete'
        assert client.delete(f"/api/memories/{target['id']}").json()['status']=='complete'
        assert len(client.get('/api/usage').json())==count
        remaining=client.get('/api/memories').json()
        assert len(remaining)==1 and remaining[0]['id']==other['id']
        original=client.get(f'/api/conversations/{source}').json()
        assert original['messages'][0]['content']=='请记住：我喜欢简洁的中文回答'
        assert original['messages'][1]['memory_action']['status']=='deleted'
        assert original['messages'][1]['memory_action']['fact']==''
        client.put('/api/settings/budget',json={'monthly_limit':None})
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        assert client.get('/api/memories').json()[0]['id']==other['id']
        conversation=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{conversation}/messages',json={'content':'按我的偏好回答'})
        assert '我喜欢简洁的中文回答' not in response.json()['messages'][-1]['content']
        assert client.delete(f"/api/memories/{target['id']}").json()['status']=='complete'


def test_deleted_candidate_and_old_source_cannot_restore_but_sibling_can_confirm(database_url: str, tmp_path: Path) -> None:
    """删除同源候选之一后，旧卡与旧发现失效，另一个独立候选仍可明确确认。"""
    import json
    from test_memory_candidates import candidate_provider
    def provider(request: httpx.Request) -> httpx.Response:
        """仅在外部模型边界为同一当前消息提出两个独立事实。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
            items=[{'fact':fact,'quote':fact,'subject':'user','target_id':None} for fact in ['我喜欢中文回答','我住在苏州']]
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'candidates':items},ensure_ascii=False)}}],'usage':{'prompt_tokens':8,'completion_tokens':4,'total_tokens':12}})
        return candidate_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        answer=client.post(f'/api/conversations/{source}/messages',json={'content':'我喜欢中文回答。我住在苏州'}).json()['messages'][-1]
        first=next(row for row in answer['memory_candidates'] if row['fact']=='我喜欢中文回答')
        second=next(row for row in answer['memory_candidates'] if row['fact']=='我住在苏州')
        assert client.post(f"/api/memory-candidates/{first['id']}/confirm").status_code==200
        entry=client.get('/api/memories').json()[0]
        update=client.put(f"/api/memories/{entry['id']}",json={'fact':'我喜欢简洁的中文回答','expected_version':1}).json()
        assert client.delete(f"/api/memories/{entry['id']}").status_code==200
        count=len(client.get('/api/usage').json())
        assert client.post(f"/api/memory-candidates/{first['id']}/confirm").status_code==409
        assert client.post(f"/api/conversations/{source}/messages/{answer['reply_to']}/memory-candidates/retry").status_code==409
        assert client.post(f"/api/memory-updates/{update['id']}/retry").status_code in (404,409)
        assert len(client.get('/api/usage').json())==count
        cards=client.get(f'/api/conversations/{source}').json()['messages'][-1]['memory_candidates']
        removed=next(row for row in cards if row['id']==first['id'])
        assert removed['fact']=='' and removed['source_quote']=='' and removed['status']=='expired'
        sibling=next(row for row in cards if row['id']==second['id'])
        assert sibling['fact']=='我住在苏州' and sibling['status']=='pending'
        assert client.post(f"/api/memory-candidates/{second['id']}/confirm").status_code==200
        assert [row['fact'] for row in client.get('/api/memories').json()]==['我住在苏州']


def test_explicit_chat_delete_is_local_and_ambiguous_request_only_clarifies(database_url: str, tmp_path: Path) -> None:
    """明确完整事实可从聊天删除；含糊或混合写作指令只澄清，零预算不阻挡本地动作。"""
    from test_streaming import wait_message
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        for fact in ['我喜欢简洁的中文回答','我住在苏州']:
            client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：'+fact})
        count=len(client.get('/api/usage').json())
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        clarification=client.post(f'/api/conversations/{source}/messages',json={'content':'忘记记忆：那个事情'})
        assert clarification.status_code==200
        assert '未删除' in clarification.json()['messages'][-1]['content']
        assert len(client.get('/api/memories').json())==2
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'删除记忆：我喜欢简洁的中文回答'})
        assert response.status_code==200
        assert '已忘记' in response.json()['messages'][-1]['content']
        entry=client.get('/api/memories').json()[0]
        started=client.post(f'/api/conversations/{source}/runs',json={'content':f"忘记记忆 {entry['id']}"})
        assert started.status_code==202
        finished=wait_message(client,source,'complete')
        assert finished['status']=='complete' and '原聊天仍保留' in finished['content']
        assert client.get('/api/memories').json()==[]
        assert len(client.get('/api/usage').json())==count


def test_history_purge_failure_stays_inactive_and_retries_after_restart(database_url: str, tmp_path: Path) -> None:
    """真实历史库拒绝清理时不报告成功，零预算重启重试仍清除向量与全部历史且不复活旧授权。"""
    import psycopg
    transport=httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        entry=client.get('/api/memories').json()[0]
        client.put(f"/api/memories/{entry['id']}",json={'fact':'我住在苏州','expected_version':1})
        with psycopg.connect(database_url) as connection:
            connection.execute("CREATE FUNCTION reject_purge() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'private history error'; END $$")
            connection.execute('CREATE TRIGGER reject_purge BEFORE DELETE ON mem0_history FOR EACH ROW EXECUTE FUNCTION reject_purge()')
        failed=client.delete(f"/api/memories/{entry['id']}")
        assert failed.status_code==503 and 'private history error' not in failed.text
        pending=client.get('/api/memories').json()[0]
        assert pending['status']=='deleting' and pending['fact']=='' and pending['deletion']['status']=='failed'
        assert pending['updates'][0]['fact']=='' and pending['updates'][0]['previous_fact']==''
        newer=client.post('/api/conversations',json={}).json()['id']
        answer=client.post(f'/api/conversations/{newer}/messages',json={'content':'我住在哪里'}).json()['messages'][-1]
        assert answer['memory_recall']['status']=='skipped'
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
    with psycopg.connect(database_url) as connection:
        connection.execute('DROP TRIGGER reject_purge ON mem0_history')
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        calls=len(client.get('/api/usage').json())
        assert client.delete(f"/api/memories/{entry['id']}").json()['status']=='complete'
        assert len(client.get('/api/usage').json())==calls
        assert client.get('/api/memories').json()==[]
    # 真实 SDK 适配契约：仅公开列表不可证明 SDK 已物理清除内部历史。
    from general_agent.database import Database
    from general_agent.mem0_adapter import PgMemory
    from general_agent.settings import StoredEmbeddingSettings
    sdk=PgMemory(Database(database_url),StoredEmbeddingSettings(base_url='https://embed.example/v1',model='embedding',dimensions=3,api_key=''),None,None)
    try:
        assert sdk.get(entry['memory_id']) is None and sdk.history(entry['memory_id'])==[]
    finally:
        sdk.close()


def test_delete_reconciles_sdk_partial_save_by_action_identity(database_url: str, tmp_path: Path) -> None:
    """正式向量已提交但业务没有 memory_id 时，删除按稳定动作身份找到实体且旧保存重试不能复活。"""
    import psycopg
    from test_streaming import wait_message
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        with psycopg.connect(database_url) as connection:
            connection.execute("CREATE FUNCTION reject_add_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'history down'; END $$")
            connection.execute('CREATE TRIGGER reject_add_history BEFORE INSERT ON mem0_history FOR EACH ROW EXECUTE FUNCTION reject_add_history()')
        client.post(f'/api/conversations/{source}/runs',json={'content':'请记住：我住在杭州'})
        failed=wait_message(client,source,'failed')
        entry=client.get('/api/memories').json()[0]
        assert entry['memory_id'] is None
        with psycopg.connect(database_url) as connection:
            connection.execute('DROP TRIGGER reject_add_history ON mem0_history')
        count=len(client.get('/api/usage').json())
        assert client.delete(f"/api/memories/{entry['id']}").json()['status']=='complete'
        client.post(f"/api/conversations/{source}/messages/{failed['id']}/retry")
        retry=wait_message(client,source,'failed')
        assert '已删除' in retry['error'] and client.get('/api/memories').json()==[]
        assert len(client.get('/api/usage').json())==count
        with psycopg.connect(database_url) as connection:
            vectors=connection.execute('SELECT count(*) FROM personal_memories').fetchone()
            assert vectors and vectors[0]==0
            histories=connection.execute('SELECT count(*) FROM mem0_history').fetchone()
            assert histories and histories[0]==0


def test_delete_during_inflight_update_cannot_be_overwritten_by_late_result(database_url: str, tmp_path: Path) -> None:
    """更正嵌入仍等待时删除立即停用；迟到响应不会覆盖墓碑或使 SDK 正文留存。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep
    from test_streaming import wait_message
    entered,release=Event(),Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """只阻塞更正事实的外部嵌入响应，保留实际调用路径及用量。"""
        if request.url.path.endswith('/embeddings') and '我住在苏州' in request.content.decode():
            entered.set()
            assert release.wait(10)
        return recall_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        entry=client.get('/api/memories').json()[0]
        client.post(f'/api/conversations/{source}/runs',json={'content':'更正记忆：我住在杭州 => 我住在苏州'})
        assert entered.wait(5)
        with ThreadPoolExecutor() as pool:
            deletion=pool.submit(client.delete,f"/api/memories/{entry['id']}")
            try:
                deadline=monotonic()+5
                while monotonic()<deadline:
                    row=client.get('/api/memories').json()[0]
                    if row['status']=='deleting':
                        break
                    sleep(.02)
                assert row['status']=='deleting' and row['fact']==''
                assert not deletion.done()
            finally:
                release.set()
            assert deletion.result(timeout=10).json()['status']=='complete'
        stopped=wait_message(client,source,'stopped')
        assert stopped['memory_update']['status']=='deleted'
        assert client.get('/api/memories').json()==[]
        assert len(client.get('/api/usage').json())==3


def test_delete_clears_search_approval_and_summary_cache_without_erasing_original_chat(database_url: str, tmp_path: Path) -> None:
    """记忆派生搜索批准与全文最终缓存立即清空，旧预算许可拒绝；原附件分段和历史聊天仍保留。"""
    import json
    import psycopg
    from test_search import reply
    from test_summary import summary_response
    from test_streaming import wait_message
    def provider(request: httpx.Request) -> httpx.Response:
        """嵌入用稳定真实协议，搜索规划含记忆正文；未经许可的搜索请求直接视为泄漏。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return recall_provider(request)
        assert request.url.host!='search.example'
        if payload['messages'][0]['content'].startswith('为明确联网请求生成查询'):
            return reply(payload,json.dumps({'query':'苏州附近咖啡店'},ensure_ascii=False))
        return summary_response(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在苏州'})
        entry=client.get('/api/memories').json()[0]
        summary=client.post('/api/conversations',json={}).json()['id']
        attachment=client.post(f'/api/conversations/{summary}/attachments',files={'files':('报告.txt','周五完成发布。'.encode())}).json()[0]
        answer=client.post(f'/api/conversations/{summary}/messages',json={'content':'请完整总结这份资料','attachment_ids':[attachment['id']]}).json()['messages'][-1]
        assert answer['status']=='complete' and answer['memory_recall']['count']==1
        search=client.post('/api/conversations',json={}).json()['id']
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        paused=client.post(f'/api/conversations/{search}/messages',json={'content':'联网搜索：根据长期记忆查住址附近咖啡店'}).json()['messages'][-1]
        action=paused['search_action']
        assert action['query']=='苏州附近咖啡店'
        decision=f"/api/conversations/{search}/search/{action['id']}/decision"
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        assert client.post(decision,json={'revision':action['revision'],'decision':'approve'}).status_code==200
        paused=wait_message(client,search,'paused')
        pause=paused['budget_pause']
        assert pause
        calls=len(client.get('/api/usage').json())
        assert client.delete(f"/api/memories/{entry['id']}").status_code==200
        stopped=client.get(f'/api/conversations/{search}').json()['messages'][-1]
        assert stopped['status']=='stopped' and stopped['budget_pause'] is None
        assert stopped['search_action']['status']=='expired' and stopped['search_action']['query']=='' and stopped['search_action']['sources']==[]
        assert client.post(decision,json={'revision':action['revision'],'decision':'approve'}).status_code==409
        assert client.post(f"/api/budget/operations/{pause['operation_id']}/approve",json={'pause_id':pause['pause_id']}).status_code==409
        assert client.post(f"/api/conversations/{search}/messages/{paused['id']}/resume").status_code==409
        assert len(client.get('/api/usage').json())==calls
        original=client.get(f'/api/conversations/{summary}').json()['messages'][-1]
        assert original['content']==answer['content'] and original['citations']==[]
        assert client.get(f'/api/conversations/{summary}/attachments').json()[0]['id']==attachment['id']
        # 隐藏持久缓存不能只凭界面推定删除，直接验证其正文清除及无关原文分段保留。
        with psycopg.connect(database_url) as connection:
            cached=connection.execute('SELECT final_content FROM document_jobs').fetchone()
            assert cached and cached[0] is None
            steps=connection.execute("SELECT count(*) FROM document_steps WHERE status='complete'").fetchone()
            assert steps and steps[0]>0
            private=connection.execute("SELECT count(*) FROM cloud_operations WHERE result::text LIKE '%苏州%' OR approved OR pause_id IS NOT NULL").fetchone()
            assert private and private[0]==0
