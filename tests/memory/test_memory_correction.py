"""通过公开接口验证记忆更正、来源与真实 Mem0 同步状态。"""

from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.auth.support import authenticated_client
from tests.memory.test_memory import configure_memory
from tests.memory.test_memory_recall import recall_provider
def test_manage_edit_keeps_identity_source_and_replaces_recall_after_restart(database_url: str, tmp_path: Path) -> None:
    """按条目标识编辑保留原来源，重放不重复更新，重启后新会话仅使用新值。"""
    transport=httpx.MockTransport(recall_provider)
    with authenticated_client(database_url,tmp_path,transport) as client:
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
        usage_before = len(client.get('/api/usage').json())
        assert client.put(path,json=body,headers=headers).status_code==200
        assert len(client.get('/api/usage').json())==usage_before
        assert client.put(path,json={'fact':'复用标识改成别的内容','expected_version':1},headers=headers).status_code==409
        assert client.put(path,json={'fact':'错改其他城市','expected_version':1}).status_code==409
        assert len(client.get('/api/memories').json())==1
    with authenticated_client(database_url,tmp_path,transport) as client:
        new=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{new}/messages',json={'content':'我现在住在哪个城市'})
        answer=response.json()['messages'][-1]
        assert '我住在苏州' in answer['content'] and '我住在杭州' not in answer['content']
def test_explicit_chat_correction_reuses_entry_and_links_new_source(database_url: str, tmp_path: Path) -> None:
    """同步和实际 runs 更正唯一旧事实或明确标识，保留两次来源且歧义不误改。"""
    from tests.conversation.test_streaming import wait_message
    with authenticated_client(database_url,tmp_path,httpx.MockTransport(recall_provider)) as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        original=client.get('/api/memories').json()[0]
        correction=client.post('/api/conversations',json={}).json()['id']
        response=client.post(f'/api/conversations/{correction}/messages',json={'content':'请把记忆中的“我住在杭州”改为“我住在苏州”'})
        assert response.status_code==200
        answer=response.json()['messages'][-1]
        assert answer['memory_update']['status']=='complete'
        entry=client.get('/api/memories').json()[0]
        assert entry['memory_id']==original['memory_id'] and entry['fact']=='我住在苏州'
        update=entry['updates'][-1]
        assert update['source_kind']=='chat' and update['source_conversation_id']==correction
        assert update['source_message_id']==answer['reply_to']
        request=f"更正记忆 {original['id']}：我住在南京"
        assert client.post(f'/api/conversations/{correction}/runs',json={'content':request}).status_code==202
        completed=wait_message(client,correction,'complete')
        assert completed['memory_update']['status']=='complete'
        entry=client.get('/api/memories').json()[0]
        assert entry['fact']=='我住在南京' and entry['version']==3 and len(entry['updates'])==2
        response=client.post(f'/api/conversations/{correction}/messages',json={'content':'更正记忆：我的城市变了'})
        assert '具体' in response.json()['messages'][-1]['content']
        assert client.get('/api/memories').json()[0]['version']==3


def test_failed_update_hides_old_value_and_retry_does_not_increment_version_twice(database_url: str, tmp_path: Path) -> None:
    """嵌入失败时新旧事实都不冒充当前记忆，显式重试复用同一目标版本。"""
    from tests.fake_mem0 import FakeMem0
    failed_once=False
    memories = FakeMem0()

    def mem0_provider(request: httpx.Request) -> httpx.Response:
        """只让第一次更正写入失败，后续重试返回真实的官方响应形状。"""
        nonlocal failed_once
        if request.method=='PUT' and request.url.path.startswith('/memories/') and not failed_once:
            failed_once=True
            return httpx.Response(503,json={'error':'private provider body'})
        return memories.handle(request)

    with authenticated_client(database_url,tmp_path,httpx.MockTransport(recall_provider),httpx.MockTransport(mem0_provider)) as client:
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


def test_stop_update_then_retry_preserves_target_and_counts_real_calls(database_url: str, tmp_path: Path) -> None:
    """更正嵌入等待中停止不恢复旧值，重试保留同一目标及来源并重新计量实际调用。"""
    from threading import Event
    from time import monotonic, sleep
    from tests.conversation.test_streaming import wait_message
    from tests.fake_mem0 import FakeMem0
    entered,release=Event(),Event()
    memories = FakeMem0()

    def mem0_provider(request: httpx.Request) -> httpx.Response:
        """在外部更正写入前暂停，确保停止发生在正式存储变化之前。"""
        if request.method=='PUT' and request.url.path.startswith('/memories/') and not release.is_set():
            entered.set()
            assert release.wait(5)
        return memories.handle(request)

    with authenticated_client(database_url,tmp_path,httpx.MockTransport(recall_provider),httpx.MockTransport(mem0_provider)) as client:
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


def test_unclear_duplicate_and_untrusted_corrections_do_not_change_memories(database_url: str, tmp_path: Path) -> None:
    """重复旧事实不能猜测目标，转述、否定和虚构中的更正词也不取得修改授权。"""
    with authenticated_client(database_url,tmp_path,httpx.MockTransport(recall_provider)) as client:
        source=configure_memory(client)
        client.post(f'/api/conversations/{source}/messages',json={'content':'请记住：我住在杭州'})
        response=client.post(f'/api/conversations/{source}/messages',json={'content':'更正记忆：我的城市变了'})
        assert '未找到唯一' in response.json()['messages'][-1]['content']
        for content in ('不要更正记忆：我住在杭州 => 我住在苏州','朋友说：“更正记忆：我住在杭州 => 我住在苏州”','写一段故事；更正记忆：我住在杭州 => 我住在苏州'):
            client.post(f'/api/conversations/{source}/messages',json={'content':content})
        entries=client.get('/api/memories').json()
        assert len(entries)==1 and entries[0]['version']==1 and entries[0]['fact']=='我住在杭州'




