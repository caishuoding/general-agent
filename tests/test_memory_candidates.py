"""通过公开聊天和确认接口验证候选记忆的授权、来源和正式提交。"""

import json
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_memory import configure_memory
from test_memory_recall import recall_provider
from test_streaming import wait_message


def candidate_provider(request: httpx.Request) -> httpx.Response:
    """仅替换外部模型响应，识别当前事实时返回可验证的候选及原文引用。"""
    payload=json.loads(request.content)
    if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
        body={'candidates':[{'fact':'我喜欢简洁的中文回答','quote':'我喜欢简洁的中文回答','subject':'user','target_id':None}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(body,ensure_ascii=False)}}],'usage':{'prompt_tokens':11,'completion_tokens':8,'total_tokens':19}})
    return recall_provider(request)


def test_current_user_fact_is_candidate_until_confirmed_and_stays_confirmed_after_restart(database_url: str, tmp_path: Path) -> None:
    """实际 runs 只生成候选，刷新后明确确认才经 Mem0 生效，重复确认没有第二次写入。"""
    transport=httpx.MockTransport(candidate_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        assert client.post(f'/api/conversations/{source}/runs',json={'content':'我喜欢简洁的中文回答'}).status_code==202
        answer=wait_message(client,source,'complete')
        candidate=answer['memory_candidates'][0]
        assert candidate['status']=='pending' and candidate['fact']=='我喜欢简洁的中文回答'
        assert candidate['source_message_id']==answer['reply_to'] and candidate['source_conversation_id']==source
        assert candidate['subject']=='user' and candidate['target_id'] is None
        assert client.get('/api/memories').json()==[]
        before=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{before}/messages',json={'content':'按我的偏好回答'})
        assert '我喜欢简洁的中文回答' not in response.json()['messages'][-1]['content']
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        restored=client.get(f'/api/conversations/{source}').json()['messages'][-1]['memory_candidates'][0]
        assert restored['id']==candidate['id'] and restored['status']=='pending'
        count=len(client.get('/api/usage').json())
        headers={'X-Operation-ID':str(uuid4())}
        path=f"/api/memory-candidates/{candidate['id']}/confirm"
        assert client.post(path,headers=headers).status_code==200
        assert client.post(path,headers=headers).status_code==200
        assert client.post(path).status_code==200
        entries=client.get('/api/memories').json()
        assert len(entries)==1 and entries[0]['fact']=='我喜欢简洁的中文回答'
        assert entries[0]['message_id']==candidate['source_message_id']
        assert len(client.get('/api/usage').json())==count+1
        new=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{new}/messages',json={'content':'按我的偏好回答'})
        assert '我喜欢简洁的中文回答' in response.json()['messages'][-1]['content']


def test_multiple_current_facts_can_be_confirmed_independently(database_url: str, tmp_path: Path) -> None:
    """同一来源的多个候选各自批准并保留来源，不复用同一个正式动作造成冲突。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """为一个当前消息返回两个独立事实，正式存储仍由真实 Mem0 完成。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
            items=[{'fact':fact,'quote':fact,'subject':'user','target_id':None} for fact in ('我喜欢中文回答','我住在苏州')]
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'candidates':items},ensure_ascii=False)}}],'usage':{'prompt_tokens':11,'completion_tokens':8,'total_tokens':19}})
        return recall_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'我喜欢中文回答。我住在苏州'})
        candidates=response.json()['messages'][-1]['memory_candidates']
        assert len(candidates)==2
        for candidate in candidates:
            assert client.post(f"/api/memory-candidates/{candidate['id']}/confirm").status_code==200
        entries=client.get('/api/memories').json()
        assert {entry['fact'] for entry in entries}=={'我喜欢中文回答','我住在苏州'}
        assert len({entry['memory_id'] for entry in entries})==2
        assert all(entry['message_id']==candidates[0]['source_message_id'] for entry in entries)


def test_ambiguous_location_change_is_candidate_and_stale_target_cannot_be_overwritten(database_url: str, tmp_path: Path) -> None:
    """最近在某地只能提出明确目标的变更，确认前旧值有效，目标版本变化后拒绝覆盖。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """候选模型仅从服务端提供的目标快照选择当前居住条目。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
            context=json.loads(payload['messages'][1]['content'])
            item={'fact':'我住在杭州','quote':'最近在杭州','subject':'user','target_id':context['targets'][0]['id']}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'candidates':[item]},ensure_ascii=False)}}],'usage':{'prompt_tokens':11,'completion_tokens':8,'total_tokens':19}})
        return recall_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在苏州'})
        original=client.get('/api/memories').json()[0]
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'最近在杭州'})
        candidate=response.json()['messages'][-1]['memory_candidates'][0]
        assert candidate['target_id']==original['id'] and candidate['target_version']==1
        assert candidate['target_fact']=='我住在苏州' and candidate['status']=='pending'
        assert client.get('/api/memories').json()[0]['fact']=='我住在苏州'
        client.put(f"/api/memories/{original['id']}",json={'fact':'我住在南京','expected_version':1})
        rejected=client.post(f"/api/memory-candidates/{candidate['id']}/confirm")
        assert rejected.status_code==409
        assert client.get('/api/memories').json()[0]['fact']=='我住在南京'
        stale=client.get(f'/api/conversations/{source}').json()['messages'][-1]['memory_candidates'][0]
        assert stale['status']=='expired'
        # 原目标未再次改变的新候选，确认才以同一正式标识更新。
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'最近在杭州'})
        fresh=response.json()['messages'][-1]['memory_candidates'][0]
        assert client.post(f"/api/memory-candidates/{fresh['id']}/confirm").status_code==200
        entry=client.get('/api/memories').json()[0]
        assert entry['fact']=='我住在杭州' and entry['memory_id']==original['memory_id'] and entry['version']==3
        assert entry['updates'][-1]['source_message_id']==fresh['source_message_id']


def test_rejected_or_expired_candidate_never_becomes_formal_memory(database_url: str, tmp_path: Path) -> None:
    """拒绝可重复执行，拒绝后或服务端有效期已过的候选不能再次确认写入。"""
    import psycopg
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(candidate_provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'我喜欢简洁的中文回答'})
        candidate=response.json()['messages'][-1]['memory_candidates'][0]
        path=f"/api/memory-candidates/{candidate['id']}"
        assert client.post(path+'/reject').status_code==200
        assert client.post(path+'/reject').status_code==200
        count=len(client.get('/api/usage').json())
        assert client.post(path+'/confirm').status_code==409
        assert len(client.get('/api/usage').json())==count
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'我喜欢简洁的中文回答'})
        expiring=response.json()['messages'][-1]['memory_candidates'][0]
        # 在数据库边界安排时间已过的状态，不替换应用内部时钟或确认逻辑。
        with psycopg.connect(database_url) as connection:
            connection.execute("UPDATE memory_candidates SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE id=%s",(expiring['id'],))
        assert client.post(f"/api/memory-candidates/{expiring['id']}/confirm").status_code==409
        assert client.get('/api/memories').json()==[]
        messages=client.get(f'/api/conversations/{source}').json()['messages']
        states=[message['memory_candidates'][0]['status'] for message in messages if message['memory_candidates']]
        assert states==['rejected','expired']


def test_discovery_failure_is_visible_and_explicit_retry_uses_only_original_user_source(database_url: str, tmp_path: Path) -> None:
    """提取失败不阻止普通回答，用户可明确重试原用户来源，失败用量仍计入。"""
    failed_once=False
    def provider(request: httpx.Request) -> httpx.Response:
        """在模型 HTTP 边界仅让首次候选发现失败，不影响普通回答。"""
        nonlocal failed_once
        payload=json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content'] and not failed_once:
            failed_once=True
            return httpx.Response(503,json={'error':'private-error','usage':{'prompt_tokens':4,'completion_tokens':0,'total_tokens':4}})
        return candidate_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'我喜欢简洁的中文回答'})
        answer=response.json()['messages'][-1]
        assert answer['status']=='complete' and answer['memory_discovery']['status']=='failed'
        assert answer['memory_candidates']==[] and 'private-error' not in response.text
        path=f"/api/conversations/{source}/messages/{answer['reply_to']}/memory-candidates/retry"
        operation={'X-Operation-ID':str(uuid4())}
        assert client.post(path,headers=operation).status_code==200
        assert client.post(path,headers=operation).status_code==200
        updated=client.get(f'/api/conversations/{source}').json()['messages'][-1]
        assert updated['memory_discovery']['status']=='complete' and len(updated['memory_candidates'])==1
        assert client.get('/api/memories').json()==[]
        assert len(client.get('/api/usage').json())==4
        assert client.post(f"/api/conversations/{source}/messages/{answer['id']}/memory-candidates/retry").status_code==409


def test_candidate_submission_failure_requires_new_retry_and_keeps_identity(database_url: str, tmp_path: Path) -> None:
    """正式提交失败可见，同一确认请求重放不能再次计费，新的明确重试复用原候选身份。"""
    fail_embedding=True
    def provider(request: httpx.Request) -> httpx.Response:
        """仅候选确认的第一次事实嵌入失败，能力检查和提取正常。"""
        nonlocal fail_embedding
        if request.url.path.endswith('/embeddings') and '我喜欢简洁的中文回答' in request.content.decode() and fail_embedding:
            fail_embedding=False
            return httpx.Response(503,json={'error':'private failed embedding','usage':{'prompt_tokens':4,'total_tokens':4}})
        return candidate_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'我喜欢简洁的中文回答'})
        candidate=response.json()['messages'][-1]['memory_candidates'][0]
        path=f"/api/memory-candidates/{candidate['id']}/confirm"
        headers={'X-Operation-ID':str(uuid4())}
        assert client.post(path,headers=headers).status_code==409
        count=len(client.get('/api/usage').json())
        assert client.post(path,headers=headers).status_code==409
        assert len(client.get('/api/usage').json())==count
        failed=client.get(f'/api/conversations/{source}').json()['messages'][-1]['memory_candidates'][0]
        assert failed['status']=='failed' and 'private' not in failed['error']
        assert client.get('/api/memories').json()[0]['status']=='failed'
        assert client.post(path).status_code==200
        entries=client.get('/api/memories').json()
        assert len(entries)==1 and entries[0]['status']=='complete'
        assert len(client.get('/api/usage').json())==count+1


def test_candidate_budget_steps_resume_after_restart_without_reextracting(database_url: str, tmp_path: Path) -> None:
    """提取、普通回答和确认嵌入分别暂停，跨重启续跑不重复提取或提前保存。"""
    transport=httpx.MockTransport(candidate_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        assert client.post(f'/api/conversations/{source}/runs',json={'content':'我喜欢简洁的中文回答'}).status_code==202
        paused=wait_message(client,source,'paused')
        assert paused['memory_candidates']==[] and paused['memory_discovery']['status']=='pending'
        assert len(client.get('/api/usage').json())==1
        pause=paused['budget_pause']
        client.post(f"/api/budget/operations/{pause['operation_id']}/approve",json={'pause_id':pause['pause_id']})
        client.post(f"/api/conversations/{source}/messages/{paused['id']}/resume")
        paused=wait_message(client,source,'paused')
        candidate=paused['memory_candidates'][0]
        assert candidate['status']=='pending' and client.get('/api/memories').json()==[]
        assert len(client.get('/api/usage').json())==2
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        pause=paused['budget_pause']
        client.post(f"/api/budget/operations/{pause['operation_id']}/approve",json={'pause_id':pause['pause_id']})
        client.post(f"/api/conversations/{source}/messages/{paused['id']}/resume")
        assert wait_message(client,source,'complete')['memory_candidates'][0]['id']==candidate['id']
        assert len(client.get('/api/usage').json())==3
        path=f"/api/memory-candidates/{candidate['id']}/confirm"
        headers={'X-Operation-ID':str(uuid4())}
        response=client.post(path,headers=headers)
        assert response.status_code==402
        pause=response.json()
        current=client.get(f'/api/conversations/{source}').json()['messages'][-1]['memory_candidates'][0]
        assert current['status']=='paused'
        assert client.get('/api/memories').json()[0]['status']=='pending'
        assert len(client.get('/api/usage').json())==3
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        client.post(f"/api/budget/operations/{pause['operation_id']}/approve",json={'pause_id':pause['pause_id']})
        assert client.post(path,headers=headers).json()['status']=='confirmed'
        assert len(client.get('/api/usage').json())==4
        assert client.get('/api/memories').json()[0]['status']=='complete'


def test_stopped_discovery_keeps_usage_and_requires_new_explicit_attempt(database_url: str, tmp_path: Path) -> None:
    """停止已发出的提取后保留实际用量但不创建候选，重试回答使用同一用户来源。"""
    from threading import Event
    started,release=Event(),Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """在外部候选响应前提供确定的停止窗口，返回真实使用量。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
            started.set()
            assert release.wait(5)
        return candidate_provider(request)
    transport=httpx.MockTransport(provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        response=client.post(f'/api/conversations/{source}/runs',json={'content':'我喜欢简洁的中文回答'})
        answer=response.json()['messages'][-1]
        assert started.wait(3)
        try:
            assert client.post(f"/api/conversations/{source}/messages/{answer['id']}/stop").status_code==200
        finally:
            release.set()
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        stopped=client.get(f'/api/conversations/{source}').json()['messages'][-1]
        assert stopped['status']=='stopped' and stopped['memory_candidates']==[]
        assert client.get('/api/memories').json()==[]
        assert len(client.get('/api/usage').json())==2
        assert client.post(f"/api/conversations/{source}/messages/{answer['id']}/retry").status_code==202
        complete=wait_message(client,source,'complete')
        assert complete['reply_to']==answer['reply_to'] and len(complete['memory_candidates'])==1
        assert len(client.get('/api/usage').json())==4


def test_discovery_reads_only_current_user_source_not_history_or_attachments(database_url: str, tmp_path: Path) -> None:
    """自动提取请求只含当前用户事实及获准目标，不读旧消息、助手回答或附件素材。"""
    discoveries: list[dict[str,object]]=[]
    def provider(request: httpx.Request) -> httpx.Response:
        """记录外部提取输入以验证来源边界，不替换应用检索或候选方法。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
            discoveries.append(payload)
        return candidate_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        client.post(f'/api/conversations/{old}/messages',json={'content':'旧会话秘密5829'})
        current=client.post('/api/conversations',json={}).json()['id']
        attachment=client.post(f'/api/conversations/{current}/attachments',files={'files':('资料.txt','附件中的他人秘密6138'.encode())}).json()[0]['id']
        client.post(f'/api/conversations/{current}/messages',json={'content':'请读取资料','attachment_ids':[attachment]})
        response=client.post(f'/api/conversations/{current}/messages',json={'content':'我喜欢简洁的中文回答','attachment_ids':[attachment]})
        assert len(response.json()['messages'][-1]['memory_candidates'])==1
        assert len(discoveries)==1
        sent=json.dumps(discoveries[0],ensure_ascii=False)
        assert '5829' not in sent and '6138' not in sent and 'attachment_id' not in sent
        assert client.get('/api/memories').json()==[]
        for content in ['请总结附件中的个人信息','搜索我喜欢的餐厅','朋友说我喜欢茶','我喜欢茶吗？','写一个故事；我住在杭州']:
            client.post(f'/api/conversations/{current}/messages',json={'content':content})
        assert len(discoveries)==1


def test_unverifiable_model_candidates_are_visible_failures_without_mem0_writes(database_url: str, tmp_path: Path) -> None:
    """模型返回其他人物、伪造来源或未知目标时，不创建可批准候选或正式记忆。"""
    cases=[{'subject':'another-person'},{'quote':'助手说我住在杭州'},{'target_id':str(uuid4())}]
    def provider(request: httpx.Request) -> httpx.Response:
        """依次返回不同类型的不可信模型字段，其他外部调用正常。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
            item={'fact':'我喜欢简洁的中文回答','quote':'我喜欢简洁的中文回答','subject':'user','target_id':None,**cases.pop(0)}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'candidates':[item]},ensure_ascii=False)}}],'usage':{'prompt_tokens':11,'completion_tokens':8,'total_tokens':19}})
        return recall_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        for _ in range(3):
            response=client.post(f'/api/conversations/{source}/messages',json={'content':'我喜欢简洁的中文回答'})
            answer=response.json()['messages'][-1]
            assert answer['status']=='complete' and answer['memory_candidates']==[]
            assert answer['memory_discovery']['status']=='failed'
        assert client.get('/api/memories').json()==[]
        assert len(client.get('/api/usage').json())==7


def test_discovery_retry_failure_replay_does_not_send_another_cloud_request(database_url: str, tmp_path: Path) -> None:
    """多次主动重试都失败后，重放任一旧操作仍返回原失败，不偷偷再调用模型。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """让候选提取持续失败，仍正常生成普通回答以隔离重试计费行为。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/chat/completions') and 'memory_candidate_discovery' in payload['messages'][0]['content']:
            return httpx.Response(503,json={'usage':{'prompt_tokens':4,'total_tokens':4}})
        return recall_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        source=configure_memory(client)
        answer=client.post(f'/api/conversations/{source}/messages',json={'content':'我喜欢简洁的中文回答'}).json()['messages'][-1]
        path=f"/api/conversations/{source}/messages/{answer['reply_to']}/memory-candidates/retry"
        first,second={'X-Operation-ID':str(uuid4())},{'X-Operation-ID':str(uuid4())}
        assert client.post(path,headers=first).status_code==409
        assert client.post(path,headers=second).status_code==409
        count=len(client.get('/api/usage').json())
        assert client.post(path,headers=first).status_code==409
        assert len(client.get('/api/usage').json())==count
