"""通过公开聊天验证真实 Mem0 的相关召回、批准范围和预算检查点。"""

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_memory import configure_memory
from test_streaming import wait_message


def recall_provider(request: httpx.Request) -> httpx.Response:
    """在云端边界提供确定的语义向量，回答原样包含收到的材料便于核对泄漏。"""
    payload = json.loads(request.content)
    if request.url.path.endswith('/embeddings'):
        data=[]
        for index,text in enumerate(payload['input']):
            vector = [1.,0.,0.] if any(word in text for word in ('回答','中文','风格')) else [0.,1.,0.]
            data.append({'index':index,'embedding':vector})
        return httpx.Response(200,json={'model':'embed','data':data,'usage':{'prompt_tokens':7,'total_tokens':7}})
    content = json.dumps(payload['messages'],ensure_ascii=False)
    if payload.get('stream'):
        return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'content':content},'finish_reason':'stop'}]},ensure_ascii=False)+'\n\ndata: '+json.dumps({'choices':[],'usage':{'prompt_tokens':9,'completion_tokens':2,'total_tokens':11}})+'\n\ndata: [DONE]\n\n')
    return httpx.Response(200,json={'choices':[{'message':{'content':content}}],'usage':{'prompt_tokens':9,'completion_tokens':2,'total_tokens':11}})


def test_new_conversation_reuses_only_related_approved_memories_after_restart(database_url: str, tmp_path: Path) -> None:
    """新会话及重启后使用相关事实，排除无关记忆、旧会话消息和未批准陈述。"""
    transport=httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        attachment=client.post(f'/api/conversations/{old}/attachments',files={'files':('旧资料.txt','旧附件专属内容583029'.encode())}).json()[0]['id']
        for content in ('请记住：我喜欢简洁的中文回答','请记住：我住在杭州','我的未批准门禁码是927481'):
            selected=[attachment] if '927481' in content else []
            assert client.post(f'/api/conversations/{old}/messages',json={'content':content,'attachment_ids':selected}).status_code==200
        new=client.post('/api/conversations',json={}).json()['id']
        assert client.post(f'/api/conversations/{new}/runs',json={'content':'按我的偏好给我一段回答风格示例'}).status_code==202
        answer=wait_message(client,new,'complete')
        assert '我喜欢简洁的中文回答' in answer['content']
        assert '杭州' not in answer['content'] and '927481' not in answer['content'] and '583029' not in answer['content']
        assert answer['memory_recall']['status']=='complete' and answer['memory_recall']['count']==1
        assert len(client.get('/api/memories').json())==2
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        new=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{new}/messages',json={'content':'请用我喜欢的回答风格介绍你自己'})
        assert response.status_code==200
        assert '我喜欢简洁的中文回答' in response.json()['messages'][-1]['content']
        assert '杭州' not in response.json()['messages'][-1]['content']
        assert len(client.get('/api/memories').json())==2


def test_recall_budget_checkpoint_survives_restart_without_duplicate_embedding(database_url: str, tmp_path: Path) -> None:
    """召回完成后的回答暂停跨重启恢复，读取标识检查点而不重复嵌入。"""
    transport=httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我喜欢简洁的中文回答'})
        new=client.post('/api/conversations',json={}).json()['id']
        client.put('/api/settings/budget',json={'monthly_limit':'0.000021','prices':[{'base_url':'https://vector.example/v1','model':'embed','input_per_million':'1'},{'base_url':'https://chat.example/v1','model':'chat','input_per_million':'1','output_per_million':'1'}]})
        assert client.post(f'/api/conversations/{new}/runs',json={'content':'按我的偏好回答'}).status_code==202
        paused=wait_message(client,new,'paused')
        assert paused['memory_recall']['status']=='complete' and paused['memory_recall']['count']==1
        assert len(client.get('/api/usage').json())==3
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        pause=paused['budget_pause']
        assert client.post(f"/api/budget/operations/{pause['operation_id']}/approve",json={'pause_id':pause['pause_id']}).status_code==200
        assert client.post(f"/api/conversations/{new}/messages/{paused['id']}/resume").status_code==200
        complete=wait_message(client,new,'complete')
        assert '我喜欢简洁的中文回答' in complete['content']
        calls=client.get('/api/usage').json()
        assert len(calls)==4
        assert len([call for call in calls if call['unit']=='embedding_tokens'])==3


def test_changed_or_removed_checkpoint_memory_is_not_sent_on_resume(database_url: str, tmp_path: Path) -> None:
    """预算暂停后正式存储条目被移除时，恢复重新核验且不沿检查点发送旧正文。"""
    from general_agent.database import Database
    from general_agent.embedding import EmbeddingConfiguration
    from general_agent.memory import MemoryService
    from general_agent.settings import EmbeddingSettingsStore
    import psycopg
    app=create_app(database_url,tmp_path,httpx.MockTransport(recall_provider))
    with TestClient(app,base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我喜欢简洁的中文回答'})
        entry=client.get('/api/memories').json()[0]
        new=client.post('/api/conversations',json={}).json()['id']
        client.put('/api/settings/budget',json={'monthly_limit':'0.000021','prices':[{'base_url':'https://vector.example/v1','model':'embed','input_per_million':'1'},{'base_url':'https://chat.example/v1','model':'chat','input_per_million':'1','output_per_million':'1'}]})
        client.post(f'/api/conversations/{new}/runs',json={'content':'按我的偏好回答'})
        paused=wait_message(client,new,'paused')
        # 尚无删除产品API；通过已批准适配契约与数据库边界安排后续生命周期前置状态。
        database=Database(database_url)
        embedding=EmbeddingConfiguration(database,EmbeddingSettingsStore(tmp_path))
        memory=MemoryService(database,embedding,app.state.calls,tmp_path)
        with memory.session(embedding.verified()) as sdk:
            sdk.delete(entry['memory_id'])
            sdk.db.purge(entry['memory_id'])
        with psycopg.connect(database_url) as connection:
            connection.execute("UPDATE memory_actions SET status='failed',version=version+1 WHERE id=%s",(entry['id'],))
        pause=paused['budget_pause']
        assert client.post(f"/api/budget/operations/{pause['operation_id']}/approve",json={'pause_id':pause['pause_id']}).status_code==200
        assert client.post(f"/api/conversations/{new}/messages/{paused['id']}/resume").status_code==200
        complete=wait_message(client,new,'complete')
        assert '我喜欢简洁的中文回答' not in complete['content']
        assert complete['memory_recall']['count']==0
        assert len(client.get('/api/usage').json())==4


def test_incomplete_mem0_write_is_not_recalled_as_approved(database_url: str, tmp_path: Path) -> None:
    """真实 Mem0 中留下的部分提交向量，即使高度相关，也不能绕过业务完成状态。"""
    import psycopg
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider)),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        with psycopg.connect(database_url) as connection:
            connection.execute("CREATE FUNCTION reject_recall_fixture_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'history unavailable'; END $$")
            connection.execute('CREATE TRIGGER reject_recall_fixture_history BEFORE INSERT ON mem0_history FOR EACH ROW EXECUTE FUNCTION reject_recall_fixture_history()')
        assert client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我喜欢简洁的中文回答'}).status_code==502
        with psycopg.connect(database_url) as connection:
            connection.execute('DROP TRIGGER reject_recall_fixture_history ON mem0_history')
        client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我住在杭州'})
        new=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{new}/messages',json={'content':'按我的偏好回答'})
        answer=response.json()['messages'][-1]
        assert '我喜欢简洁的中文回答' not in answer['content'] and '杭州' not in answer['content']
        assert answer['memory_recall']['status']=='complete' and answer['memory_recall']['count']==0


def test_recall_failure_and_unverified_config_are_visible_without_blocking_chat(database_url: str, tmp_path: Path) -> None:
    """嵌入故障和配置未验证时明确显示未读取，按当前会话继续且不声称获得记忆。"""
    fail_recall=False
    def provider(request: httpx.Request) -> httpx.Response:
        """仅让召回嵌入失败，已保存事实和普通回答仍用真实应用路径。"""
        if fail_recall and request.url.path.endswith('/embeddings'):
            return httpx.Response(503,json={'error':'private-vector-detail','usage':{'prompt_tokens':4}})
        return recall_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我喜欢简洁的中文回答'})
        new=client.post('/api/conversations',json={}).json()['id']
        fail_recall=True
        client.post(f'/api/conversations/{new}/runs',json={'content':'按我的偏好回答'})
        answer=wait_message(client,new,'complete')
        assert answer['memory_recall']['status']=='failed'
        assert '召回失败' in answer['memory_recall']['detail']
        assert '我喜欢简洁的中文回答' not in answer['content'] and 'private-vector-detail' not in json.dumps(answer)
        calls=client.get('/api/usage').json()
        assert len(calls)==4 and any(call['status']=='failed' and call['prompt_tokens']==4 for call in calls)
        client.put('/api/settings/embedding',json={'base_url':'https://vector.example/v1','model':'embed','dimensions':3,'api_key':'rotated-test-key'})
        other=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{other}/messages',json={'content':'按我的偏好回答'})
        assert response.status_code==200
        assert response.json()['messages'][-1]['memory_recall']['status']=='unavailable'
        assert len(client.get('/api/usage').json())==5


def test_stop_recall_prevents_followup_chat_and_retry_recounts_actual_calls(database_url: str, tmp_path: Path) -> None:
    """停止等待中的召回及时生效，已发嵌入计量保留且不会继续发送回答请求。"""
    from threading import Event
    from time import monotonic,sleep
    entered,release=Event(),Event()
    waiting=False
    def provider(request: httpx.Request) -> httpx.Response:
        """让一次查询嵌入等待，测试通过公开停止接口解除后续调用许可。"""
        nonlocal waiting
        if waiting and request.url.path.endswith('/embeddings'):
            waiting=False
            entered.set()
            assert release.wait(5)
        return recall_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我喜欢简洁的中文回答'})
        new=client.post('/api/conversations',json={}).json()['id']
        waiting=True
        response=client.post(f'/api/conversations/{new}/runs',json={'content':'按我的偏好回答'})
        answer_id=response.json()['messages'][-1]['id']
        assert entered.wait(5)
        try:
            before=monotonic()
            assert client.post(f'/api/conversations/{new}/messages/{answer_id}/stop').status_code==200
            assert monotonic()-before<1
        finally:
            release.set()
        deadline=monotonic()+5
        while monotonic()<deadline:
            if client.get('/api/usage/summary').json()['pending_calls']==0:
                break
            sleep(.02)
        assert len(client.get('/api/usage').json())==3
        assert wait_message(client,new,'stopped')['content']==''
        assert client.post(f'/api/conversations/{new}/messages/{answer_id}/retry').status_code==202
        complete=wait_message(client,new,'complete')
        assert '我喜欢简洁的中文回答' in complete['content']
        assert len(client.get('/api/usage').json())==5
        assert len(client.get('/api/memories').json())==1
