"""通过公开接口验证记忆更正、来源与真实 Mem0 同步状态。"""

from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_memory import configure_memory
from test_memory_recall import recall_provider


def test_manage_edit_keeps_identity_source_and_replaces_recall_after_restart(database_url: str, tmp_path: Path) -> None:
    """按条目标识编辑保留原来源，重放不重复更新，重启后新会话仅使用新值。"""
    transport=httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        original=client.get('/api/memories').json()[0]
        path=f"/api/memories/{original['id']}"
        body={'fact':'我住在苏州','expected_version':original['version']}
        headers={'X-Operation-ID':str(uuid4())}
        edited=client.put(path,json=body,headers=headers)
        assert edited.status_code==200
        entry=client.get('/api/memories').json()[0]
        assert entry['fact']=='我住在苏州' and entry['status']=='complete'
        assert entry['id']==original['id'] and entry['memory_id']==original['memory_id']
        assert entry['message_id']==original['message_id'] and entry['conversation_id']==source
        assert entry['version']==2 and entry['updates'][-1]['source_kind']=='management'
        assert client.put(path,json=body,headers=headers).status_code==200
        assert len(client.get('/api/usage').json())==3
        assert client.put(path,json={'fact':'复用标识改成别的内容','expected_version':1},headers=headers).status_code==409
        assert client.put(path,json={'fact':'错改其他城市','expected_version':1}).status_code==409
        assert len(client.get('/api/memories').json())==1
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        new=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{new}/messages',json={'content':'我现在住在哪个城市'})
        answer=response.json()['messages'][-1]
        assert '我住在苏州' in answer['content'] and '我住在杭州' not in answer['content']


def test_explicit_chat_correction_reuses_entry_and_links_new_source(database_url: str, tmp_path: Path) -> None:
    """同步和实际 runs 更正唯一旧事实或明确标识，保留两次来源且歧义不误改。"""
    from test_streaming import wait_message
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        original=client.get('/api/memories').json()[0]
        correction=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{correction}/messages',json={'content':'请把记忆中的“我住在杭州”改为“我住在苏州”'})
        assert response.status_code==200
        answer=response.json()['messages'][-1]
        assert '已更正长期记忆' in answer['content']
        assert answer['memory_update']['status']=='complete'
        entry=client.get('/api/memories').json()[0]
        assert entry['memory_id']==original['memory_id'] and entry['fact']=='我住在苏州'
        update=entry['updates'][-1]
        assert update['source_kind']=='chat' and update['source_conversation_id']==correction
        assert update['source_message_id']==answer['reply_to']
        request=f"更正记忆 {original['id']}：我住在南京"
        assert client.post(f'/api/conversations/{correction}/runs',json={'content':request}).status_code==202
        completed=wait_message(client,correction,'complete')
        assert '已更正长期记忆' in completed['content']
        entry=client.get('/api/memories').json()[0]
        assert entry['fact']=='我住在南京' and entry['version']==3 and len(entry['updates'])==2
        response=client.post(f'/api/conversations/{correction}/messages',json={'content':'更正记忆：我的城市变了'})
        assert '具体' in response.json()['messages'][-1]['content']
        assert client.get('/api/memories').json()[0]['version']==3


def test_failed_update_hides_old_value_and_retry_does_not_increment_version_twice(database_url: str, tmp_path: Path) -> None:
    """嵌入失败时新旧事实都不冒充当前记忆，显式重试复用同一目标版本。"""
    failed_once=False
    def provider(request: httpx.Request) -> httpx.Response:
        """只让第一次更正嵌入失败，实际失败用量仍应记录。"""
        nonlocal failed_once
        if request.url.path.endswith('/embeddings') and '我住在苏州' in request.content.decode() and not failed_once:
            failed_once=True
            return httpx.Response(503,json={'error':'private provider body','usage':{'prompt_tokens':4,'total_tokens':4}})
        return recall_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        original=client.get('/api/memories').json()[0]
        result=client.put(f"/api/memories/{original['id']}",json={'fact':'我住在苏州','expected_version':1})
        assert result.status_code==409 and 'private provider body' not in result.text
        failed=client.get('/api/memories').json()[0]
        assert failed['status']=='failed' and failed['version']==2
        update=failed['updates'][-1]
        assert update['status']=='failed'
        new=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{new}/messages',json={'content':'我住在哪里'})
        assert '我住在杭州' not in response.json()['messages'][-1]['content']
        assert '我住在苏州' not in response.json()['messages'][-1]['content']
        headers={'X-Operation-ID':str(uuid4())}
        path=f"/api/memory-updates/{update['id']}/retry"
        assert client.post(path,headers=headers).status_code==200
        assert client.post(path,headers=headers).status_code==200
        entry=client.get('/api/memories').json()[0]
        assert entry['status']=='complete' and entry['version']==2 and entry['memory_id']==original['memory_id']
        assert len(entry['updates'])==1 and entry['fact']=='我住在苏州'
        embeddings=[call for call in client.get('/api/usage').json() if call['unit']=='embedding_tokens']
        assert len(embeddings)==4 and sum(call['prompt_tokens'] for call in embeddings)==25


def test_update_history_failure_reconciles_without_reembedding(database_url: str, tmp_path: Path) -> None:
    """真实历史写入故障留下待核对向量，重启后重试修复历史而不重复嵌入或版本。"""
    import psycopg
    transport=httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        original=client.get('/api/memories').json()[0]
        with psycopg.connect(database_url) as connection:
            # 模拟记忆更正历史提交失败，验证恢复核对已有向量。
            connection.execute("CREATE FUNCTION reject_update_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.event = 'UPDATE' THEN RAISE EXCEPTION 'update history unavailable'; END IF; RETURN NEW; END $$")
            connection.execute('CREATE TRIGGER reject_update_history BEFORE INSERT ON mem0_history FOR EACH ROW EXECUTE FUNCTION reject_update_history()')
        assert client.put(f"/api/memories/{original['id']}",json={'fact':'我住在苏州','expected_version':1}).status_code==409
        failed=client.get('/api/memories').json()[0]
        assert failed['status']=='failed' and failed['updates'][-1]['status']=='failed'
        with psycopg.connect(database_url) as connection:
            connection.execute('DROP TRIGGER reject_update_history ON mem0_history')
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        identifier=failed['updates'][-1]['id']
        assert client.post(f'/api/memory-updates/{identifier}/retry').status_code==200
        entry=client.get('/api/memories').json()[0]
        assert entry['fact']=='我住在苏州' and entry['status']=='complete' and entry['version']==2
        assert len(client.get('/api/usage').json())==3


def test_chat_correction_pauses_after_old_value_is_disabled_and_resumes_after_restart(database_url: str, tmp_path: Path) -> None:
    """更正预算暂停先撤下旧值，重启批准后复用原消息和目标版本，不重复更新。"""
    from test_streaming import wait_message
    transport=httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        correction=client.post('/api/conversations',json={}).json()['id']
        assert client.post(f'/api/conversations/{correction}/runs',json={'content':'更正记忆：我住在杭州 => 我住在苏州'}).status_code==202
        paused=wait_message(client,correction,'paused')
        assert paused['memory_update']['status']=='pending' and '已更正' not in paused['content']
        entry=client.get('/api/memories').json()[0]
        assert entry['status']=='pending' and entry['version']==2
        assert len(client.get('/api/usage').json())==2
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        # 单独放行一个普通回答，验证暂停期间没有旧事实材料进入模型。
        new=client.post('/api/conversations',json={}).json()['id']
        operation=str(uuid4())
        path=f'/api/conversations/{new}/messages'
        body={'content':'我住在哪里'}
        first=client.post(path,json=body,headers={'X-Operation-ID':operation})
        assert first.status_code==402
        client.post(f'/api/budget/operations/{operation}/approve',json={'pause_id':first.json()['pause_id']})
        response=client.post(path,json=body,headers={'X-Operation-ID':operation})
        assert '我住在杭州' not in response.json()['messages'][-1]['content']
        pause=paused['budget_pause']
        client.post(f"/api/budget/operations/{pause['operation_id']}/approve",json={'pause_id':pause['pause_id']})
        assert client.post(f"/api/conversations/{correction}/messages/{paused['id']}/resume").status_code==200
        complete=wait_message(client,correction,'complete')
        assert complete['memory_update']['status']=='complete' and '已更正' in complete['content']
        entry=client.get('/api/memories').json()[0]
        assert entry['version']==2 and entry['fact']=='我住在苏州'
        assert len([call for call in client.get('/api/usage').json() if call['unit']=='embedding_tokens'])==3


def test_stop_update_then_retry_preserves_target_and_counts_real_calls(database_url: str, tmp_path: Path) -> None:
    """更正嵌入等待中停止不恢复旧值，重试保留同一目标及来源并重新计量实际调用。"""
    from threading import Event
    from time import monotonic, sleep
    from test_streaming import wait_message
    entered,release=Event(),Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """在真实外部调用边界暂停一次更正响应，确保停止发生在写入之前。"""
        if request.url.path.endswith('/embeddings') and '我住在苏州' in request.content.decode() and not release.is_set():
            entered.set()
            assert release.wait(5)
        return recall_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        started=client.post(f'/api/conversations/{source}/runs',json={'content':'更正记忆：我住在杭州 => 我住在苏州'}).json()
        answer_id=started['messages'][-1]['id']
        assert entered.wait(5)
        try:
            before=monotonic()
            stopped=client.post(f'/api/conversations/{source}/messages/{answer_id}/stop').json()['messages'][-1]
            assert stopped['status']=='stopped' and monotonic()-before<1
        finally:
            release.set()
        deadline=monotonic()+5
        while monotonic()<deadline:
            entry=client.get('/api/memories').json()[0]
            if entry['status']=='failed':
                break
            sleep(.02)
        assert entry['status']=='failed' and entry['version']==2
        assert client.post(f'/api/conversations/{source}/messages/{answer_id}/retry').status_code==202
        complete=wait_message(client,source,'complete')
        assert complete['memory_update']['id']==stopped['memory_update']['id']
        assert complete['memory_update']['status']=='complete'
        entry=client.get('/api/memories').json()[0]
        assert entry['fact']=='我住在苏州' and entry['version']==2 and len(entry['updates'])==1
        usage=client.get('/api/usage').json()
        assert len(usage)==4 and all(call['unit']=='embedding_tokens' and call['prompt_tokens']==7 for call in usage)


def test_unclear_duplicate_and_untrusted_corrections_do_not_change_memories(database_url: str, tmp_path: Path) -> None:
    """重复旧事实不能猜测目标，转述、否定和虚构中的更正词也不取得修改授权。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        for _ in range(2):
            client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'更正记忆：我住在杭州 => 我住在苏州'})
        assert '未找到唯一' in response.json()['messages'][-1]['content']
        for content in ('不要更正记忆：我住在杭州 => 我住在苏州','朋友说：“更正记忆：我住在杭州 => 我住在苏州”','写一段故事；更正记忆：我住在杭州 => 我住在苏州'):
            client.post(f'/api/conversations/{source}/messages',json={'content':content})
        entries=client.get('/api/memories').json()
        assert len(entries)==2 and all(entry['version']==1 and entry['fact']=='我住在杭州' for entry in entries)


def test_management_pause_cannot_reuse_approval_after_embedding_change(database_url: str, tmp_path: Path) -> None:
    """管理页暂停绑定原嵌入配置，配置变更须新尝试且仍保留原目标版本。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        original=client.get('/api/memories').json()[0]
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        path=f"/api/memories/{original['id']}"
        body={'fact':'我住在苏州','expected_version':1}
        operation=str(uuid4())
        headers={'X-Operation-ID':operation}
        paused=client.put(path,json=body,headers=headers)
        assert paused.status_code==402
        client.post(f'/api/budget/operations/{operation}/approve',json={'pause_id':paused.json()['pause_id']})
        client.put('/api/settings/embedding',json={'base_url':'https://vector.example/v1','model':'embed','api_key':'changed-test-key','dimensions':3})
        assert client.put(path,json=body,headers=headers).status_code==409
        assert len(client.get('/api/usage').json())==2
        pending=client.get('/api/memories').json()[0]
        assert pending['status']=='pending' and pending['version']==2
        client.put('/api/settings/budget',json={'monthly_limit':None})
        assert client.post('/api/settings/capabilities/embedding').json()['status']=='passed'
        assert client.post(f"/api/memory-updates/{pending['updates'][-1]['id']}/retry").status_code==200
        entry=client.get('/api/memories').json()[0]
        assert entry['fact']=='我住在苏州' and entry['version']==2


def test_corrected_memory_invalidates_existing_recall_checkpoint(database_url: str, tmp_path: Path) -> None:
    """真实更正后续跑旧召回检查点不会发送旧值；新的会话只使用新正式值。"""
    from test_streaming import wait_message
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        original=client.get('/api/memories').json()[0]
        client.put('/api/settings/budget',json={'monthly_limit':'0.000021','prices':[{'base_url':'https://vector.example/v1','model':'embed','input_per_million':'1'},{'base_url':'https://chat.example/v1','model':'chat','input_per_million':'1','output_per_million':'1'}]})
        conversation=client.post('/api/conversations',json={}).json()['id']
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'我住在哪里'})
        paused=wait_message(client,conversation,'paused')
        assert paused['memory_recall']['count']==1
        client.put('/api/settings/budget',json={'monthly_limit':None})
        assert client.put(f"/api/memories/{original['id']}",json={'fact':'我住在苏州','expected_version':1}).status_code==200
        assert client.post(f"/api/conversations/{conversation}/messages/{paused['id']}/resume").status_code==200
        resumed=wait_message(client,conversation,'complete')
        assert '我住在杭州' not in resumed['content'] and resumed['memory_recall']['count']==0
        assert len([call for call in client.get('/api/usage').json() if call['unit']=='embedding_tokens'])==4
        new=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{new}/messages',json={'content':'我住在哪里'})
        assert '我住在苏州' in response.json()['messages'][-1]['content']
        assert '我住在杭州' not in response.json()['messages'][-1]['content']
