"""通过公开聊天验证真实 Mem0 的相关召回、有效范围和恢复检查点。"""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.memory.test_memory import configure_memory, final_model_reply, memory_final_answer, memory_provider
from tests.conversation.test_streaming import wait_message
def recall_provider(request: httpx.Request) -> httpx.Response:
    """在云端边界提供确定的语义向量，回答原样包含收到的材料便于核对泄漏。"""
    payload = json.loads(request.content)
    if request.url.path.endswith('/embeddings'):
        data=[]
        for index,text in enumerate(payload['input']):
            vector = [1.,0.,0.] if any(word in text for word in ('回答','中文','风格')) else [0.,1.,0.]
            data.append({'index':index,'embedding':vector})
        return httpx.Response(200,json={'model':'embed','data':data,'usage':{'prompt_tokens':7,'total_tokens':7}})
    if payload.get('tools'):
        return memory_provider(request)
    final = memory_final_answer(payload)
    if final is not None:
        return final_model_reply(payload, final)
    content = json.dumps(payload['messages'],ensure_ascii=False)
    if payload.get('stream'):
        return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'content':content},'finish_reason':'stop'}]},ensure_ascii=False)+'\n\ndata: '+json.dumps({'choices':[],'usage':{'prompt_tokens':9,'completion_tokens':2,'total_tokens':11}})+'\n\ndata: [DONE]\n\n')
    return httpx.Response(200,json={'choices':[{'message':{'content':content}}],'usage':{'prompt_tokens':9,'completion_tokens':2,'total_tokens':11}})
def test_new_conversation_reuses_only_related_approved_memories_after_restart(database_url: str, tmp_path: Path) -> None:
    """新会话及重启后使用相关事实，排除无关记忆、旧会话消息和未批准陈述。"""
    transport=httpx.MockTransport(recall_provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        for content in ('请记住：我喜欢简洁的中文回答','请记住：我住在杭州','我的未批准门禁码是927481'):
            assert client.post(f'/api/conversations/{old}/messages',json={'content':content}).status_code==200
        new=client.post('/api/conversations',json={}).json()['id']
        assert client.post(f'/api/conversations/{new}/runs',json={'content':'按我的偏好给我一段回答风格示例'}).status_code==202
        answer=wait_message(client,new,'complete')
        assert '我喜欢简洁的中文回答' in answer['content']
        assert '杭州' not in answer['content'] and '927481' not in answer['content']
        assert answer['memory_recall']['status']=='complete' and answer['memory_recall']['count']==1
        assert len(client.get('/api/memories').json())==2
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        new=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{new}/messages',json={'content':'请用我喜欢的回答风格介绍你自己'})
        assert response.status_code==200
        assert '我喜欢简洁的中文回答' in response.json()['messages'][-1]['content']
        assert '杭州' not in response.json()['messages'][-1]['content']
        assert len(client.get('/api/memories').json())==2






def test_recall_failure_and_unverified_config_are_visible_without_blocking_chat(database_url: str, tmp_path: Path) -> None:
    """嵌入故障和配置未验证时明确显示未读取，按当前会话继续且不声称获得记忆。"""
    from tests.fake_mem0 import FakeMem0
    fail_recall=False
    memories=FakeMem0()

    def mem0_provider(request: httpx.Request) -> httpx.Response:
        """只让一次正式检索失败，新增和后续检索仍走官方接口形状。"""
        if fail_recall and request.method=='POST' and request.url.path=='/search':
            return httpx.Response(503,json={'error':'private-vector-detail'})
        return memories.handle(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider),httpx.MockTransport(mem0_provider)),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我喜欢简洁的中文回答'})
        new=client.post('/api/conversations',json={}).json()['id']
        fail_recall=True
        client.post(f'/api/conversations/{new}/runs',json={'content':'按我的偏好回答'})
        answer=wait_message(client,new,'complete')
        assert answer['memory_recall']['status']=='failed'
        assert '召回失败' in answer['memory_recall']['detail']
        assert '我喜欢简洁的中文回答' not in answer['content'] and 'private-vector-detail' not in json.dumps(answer)
        fail_recall=False
        other=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{other}/messages',json={'content':'按我的偏好回答'})
        assert response.status_code==200
        assert response.json()['messages'][-1]['memory_recall']['status']=='complete'
        assert '我喜欢简洁的中文回答' in response.json()['messages'][-1]['content']


def test_stop_recall_prevents_followup_chat_and_retry_recounts_actual_calls(database_url: str, tmp_path: Path) -> None:
    """停止等待中的召回及时生效，已发嵌入计量保留且不会继续发送回答请求。"""
    from threading import Event
    from time import monotonic,sleep
    from tests.fake_mem0 import FakeMem0
    entered,release=Event(),Event()
    waiting=False
    memories=FakeMem0()

    def mem0_provider(request: httpx.Request) -> httpx.Response:
        """让一次正式检索等待，测试通过公开停止接口解除后续调用许可。"""
        nonlocal waiting
        if waiting and request.method=='POST' and request.url.path=='/search':
            waiting=False
            entered.set()
            assert release.wait(5)
        return memories.handle(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(recall_provider),httpx.MockTransport(mem0_provider)),base_url='http://127.0.0.1:8000') as client:
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
            if not any(row['status'] in ('reserved','generating') for row in client.get('/api/usage').json()):
                break
            sleep(.02)
        assert wait_message(client,new,'stopped')['content']==''
        assert client.post(f'/api/conversations/{new}/messages/{answer_id}/retry').status_code==202
        complete=wait_message(client,new,'complete')
        assert '我喜欢简洁的中文回答' in complete['content']
        assert len(client.get('/api/memories').json())==1
