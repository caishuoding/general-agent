"""从公开接口验证忘记记忆、原文保留与旧授权失效，正式存储使用真实 Mem0。"""

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.memory.test_memory import configure_memory, memory_provider
from tests.memory.test_memory_recall import recall_provider


def test_delete_memory_preserves_chat_and_other_entries_without_cloud_calls(database_url: str, tmp_path: Path) -> None:
    """管理页删除只忘记指定条目且不发云端请求，原聊天及其他记忆跨重启保留。"""
    transport=httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        for fact in ['我喜欢简洁的中文回答','我住在苏州']:
            assert client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：'+fact}).status_code==200
        entries=client.get('/api/memories').json()
        target=next(row for row in entries if row['fact']=='我喜欢简洁的中文回答')
        other=next(row for row in entries if row['fact']=='我住在苏州')
        count=len(client.get('/api/usage').json())
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
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        assert client.get('/api/memories').json()[0]['id']==other['id']
        conversation=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{conversation}/messages',json={'content':'按我的偏好回答'})
        assert '我喜欢简洁的中文回答' not in response.json()['messages'][-1]['content']
        assert client.delete(f"/api/memories/{target['id']}").json()['status']=='complete'
        history=client.post(f'/api/conversations/{conversation}/messages',json={'content':'查找旧聊天：简洁的中文回答'}).json()['messages'][-1]
        assert any('我喜欢简洁的中文回答' in item['excerpt'] for item in history['history_sources'])
        assert len(client.get('/api/memories').json())==1


def test_explicit_chat_delete_is_local_and_ambiguous_request_only_clarifies(database_url: str, tmp_path: Path) -> None:
    """明确完整事实可从聊天删除；含糊或混合写作指令只澄清且不触发云端调用。"""
    from tests.conversation.test_streaming import wait_message
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        for fact in ['我喜欢简洁的中文回答','我住在苏州']:
            client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：'+fact})
        count=len(client.get('/api/usage').json())
        clarification=client.post(f'/api/conversations/{source}/messages',json={'content':'忘记记忆：那个事情'})
        assert clarification.status_code==200
        assert '未删除' in clarification.json()['messages'][-1]['content']
        assert len(client.get('/api/memories').json())==2
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'删除记忆：我喜欢简洁的中文回答'})
        assert response.status_code==200
        assert len(client.get('/api/memories').json())==1
        entry=client.get('/api/memories').json()[0]
        started=client.post(f'/api/conversations/{source}/runs',json={'content':f"忘记记忆 {entry['id']}"})
        assert started.status_code==202
        finished=wait_message(client,source,'complete')
        assert finished['status']=='complete'
        assert client.get('/api/memories').json()==[]


def test_delete_during_inflight_update_cannot_be_overwritten_by_late_result(database_url: str, tmp_path: Path) -> None:
    """更正嵌入仍等待时删除立即停用；迟到响应不会覆盖墓碑或使 SDK 正文留存。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep
    from tests.conversation.test_streaming import wait_message
    from tests.fake_mem0 import FakeMem0
    entered,release=Event(),Event()
    memories = FakeMem0()

    def mem0_provider(request: httpx.Request) -> httpx.Response:
        """阻塞正式更正写入，同时让删除先完成停用。"""
        if request.method=='PUT' and request.url.path.startswith('/memories/'):
            entered.set()
            assert release.wait(10)
        return memories.handle(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider),httpx.MockTransport(mem0_provider)),base_url='http://127.0.0.1:8000') as client:
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


def test_delete_clears_search_approval_and_summary_cache_without_erasing_original_chat(database_url: str, tmp_path: Path) -> None:
    """记忆派生搜索确认与全文最终缓存立即清空；原附件分段和历史聊天仍保留。"""
    import json
    import psycopg
    from tests.search.test_search import reply
    from tests.conversation.test_summary import summary_response
    from tests.conversation.test_streaming import wait_message
    def provider(request: httpx.Request) -> httpx.Response:
        """嵌入用稳定真实协议，搜索规划含记忆正文；未经许可的搜索请求直接视为泄漏。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return recall_provider(request)
        if payload.get('tools'):
            return memory_provider(request)
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
        answer=client.post(f'/api/conversations/{summary}/messages',json={'content':'结合我的城市信息总结这份资料','attachment_ids':[attachment['id']]}).json()['messages'][-1]
        assert answer['status']=='complete' and answer['memory_recall']['count']==1
        search=client.post('/api/conversations',json={}).json()['id']
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        paused=client.post(f'/api/conversations/{search}/messages',json={'content':'联网搜索：根据长期记忆查住址附近咖啡店'}).json()['messages'][-1]
        action=paused['search_action']
        assert action['query']=='苏州附近咖啡店'
        decision=f"/api/conversations/{search}/search/{action['id']}/decision"
        calls=len(client.get('/api/usage').json())
        assert client.delete(f"/api/memories/{entry['id']}").status_code==200
        stopped=client.get(f'/api/conversations/{search}').json()['messages'][-1]
        assert stopped['status']=='stopped'
        assert stopped['search_action']['status']=='expired' and stopped['search_action']['query']=='' and stopped['search_action']['sources']==[]
        assert client.post(decision,json={'revision':action['revision'],'decision':'approve'}).status_code==409
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
            private=connection.execute("SELECT count(*) FROM cloud_operations WHERE result::text LIKE '%苏州%'").fetchone()
            assert private and private[0]==0


def test_deleted_memory_cannot_return_through_late_summary_cache(database_url: str, tmp_path: Path) -> None:
    """删除时已发出的最终总结可以收尾计量，但晚到完整正文不得缓存给下一次明确重试。"""
    import json
    from threading import Event
    from time import monotonic, sleep
    from tests.conversation.test_summary import summary_response
    from tests.conversation.test_streaming import wait_message
    entered,release=Event(),Event()
    streams=0
    def provider(request: httpx.Request) -> httpx.Response:
        """阻塞含旧记忆的最终回答；重试模型明确返回不含旧事实的不同答案以检测缓存复用。"""
        nonlocal streams
        payload=json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return recall_provider(request)
        if payload.get('tools'):
            return memory_provider(request)
        if payload.get('stream'):
            streams+=1
            if streams==1:
                assert '我住在苏州' in json.dumps(payload,ensure_ascii=False)
                entered.set()
                assert release.wait(10)
                content='我住在苏州 [A1]'
            else:
                assert '我住在苏州' not in json.dumps(payload,ensure_ascii=False)
                content='重新整理的资料 [A1]'
            return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'content':content},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})+'\n\ndata: [DONE]\n\n')
        return summary_response(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在苏州'})
        entry=client.get('/api/memories').json()[0]
        conversation=client.post('/api/conversations',json={}).json()['id']
        file=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('报告.txt','周五完成发布。'.encode())}).json()[0]
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'结合我的城市信息总结这份资料','attachment_ids':[file['id']]})
        assert entered.wait(5)
        try:
            assert client.delete(f"/api/memories/{entry['id']}").status_code==200
            stopped=wait_message(client,conversation,'stopped')
        finally:
            release.set()
        deadline=monotonic()+5
        while any(row['status'] in ('reserved','generating') for row in client.get('/api/usage').json()) and monotonic()<deadline:
            sleep(.02)
        assert client.post(f"/api/conversations/{conversation}/messages/{stopped['id']}/retry").status_code==202
        complete=wait_message(client,conversation,'complete')
        assert streams==2 and '重新整理的资料' in complete['content'] and '我住在苏州' not in complete['content']


