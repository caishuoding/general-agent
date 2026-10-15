"""通过公开接口与真实 Mem0 验证受预算约束的索引重建。"""

import json
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_memory import configure_memory, memory_provider


def rebuild_provider(request: httpx.Request) -> httpx.Response:
    """让不同模型返回不同维度，真实 PGVector 能识别错误空间混用。"""
    payload=json.loads(request.content)
    if request.url.path.endswith('/embeddings'):
        dimensions=4 if payload['model']=='new-embed' else 3
        return httpx.Response(200,json={'model':payload['model'],'data':[{'index':i,'embedding':[1.0]+[0.0]*(dimensions-1)} for i,_ in enumerate(payload['input'])],'usage':{'prompt_tokens':7,'total_tokens':7}})
    return memory_provider(request)


def wait_rebuild(client: TestClient, identifier: str, status: str) -> dict[str, Any]:
    """等待公开持久化任务状态，失败时保留实际响应便于判断。"""
    result: dict[str, Any]={}
    deadline=monotonic()+15
    while monotonic()<deadline:
        response=client.get(f'/api/settings/embedding/rebuilds/{identifier}')
        assert response.status_code==200,response.text
        result=response.json()
        if result['status']==status:
            return result
        if result['status']=='failed' and status!='failed':
            break
        sleep(.03)
    raise AssertionError(result)


def target() -> dict[str, Any]:
    """构造与旧空间不同的明确目标，测试密钥不是实际服务凭据。"""
    return {'base_url':'https://new-vector.example/v1','model':'new-embed','dimensions':4,'api_key':'test-target'}


def test_rebuild_changes_space_preserving_approved_memory_identity(database_url: str, tmp_path: Path) -> None:
    """新空间完整可用才发布；正式条目 ID、正文和来源不因重建而变化。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(rebuild_provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        assert client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢简短回答'}).status_code==200
        before=client.get('/api/memories').json()[0]
        preview=client.post('/api/settings/embedding/rebuild-impact',json=target())
        assert preview.status_code==200,preview.text
        assert preview.json()['memory_count']==1
        assert preview.json()['requires_rebuild'] is True
        assert all(not row['affected'] for row in preview.json()['collections'] if row['kind']!='memory')
        identifier=str(uuid4())
        body={'id':identifier,'target':target(),'confirmed':True}
        response=client.post('/api/settings/embedding/rebuilds',json=body)
        assert response.status_code==202,response.text
        done=wait_rebuild(client,identifier,'complete')
        assert done['completed']==done['total']==1
        assert done['target']['model']=='new-embed'
        assert 'test-target' not in json.dumps(done)
        assert client.post('/api/settings/embedding/rebuilds',json=body).status_code==202
        after=client.get('/api/memories').json()[0]
        for field in ('id','memory_id','fact','version','message_id','conversation_id','created_at','updated_at'):
            assert after[field]==before[field]
        assert client.get('/api/settings/embedding').json()['dimensions']==4
        assert client.get('/api/settings/capabilities').json()['embedding']['status']=='passed'
        new=client.post('/api/conversations',json={}).json()['id']
        answer=client.post(f'/api/conversations/{new}/messages',json={'content':'回答应采用什么风格？'})
        assert answer.status_code==200,answer.text
        assert answer.json()['messages'][-1]['memory_recall']['count']==1
        calls=client.get('/api/usage').json()
        assert len([row for row in calls if row['purpose']=='embedding_rebuild'])==2


def approve_step(client: TestClient, identifier: str, previous: dict[str, Any]) -> dict[str, Any]:
    """只批准当前暂停的一次调用，等待该挑战被消费后的下一状态。"""
    pause=previous['pause']
    assert client.post(f"/api/budget/operations/{pause['operation_id']}/approve",json={'pause_id':pause['pause_id']}).status_code==200
    assert client.post(f'/api/settings/embedding/rebuilds/{identifier}/resume').status_code==202
    deadline=monotonic()+15
    while monotonic()<deadline:
        result: dict[str, Any]=client.get(f'/api/settings/embedding/rebuilds/{identifier}').json()
        if result['status']=='complete' or (result['status']=='paused' and result['pause']['pause_id']!=pause['pause_id']):
            return result
        sleep(.03)
    raise AssertionError(result)


def test_budget_restart_and_delete_cleans_completed_shadow_copy(database_url: str, tmp_path: Path) -> None:
    """单次许可只推进一步；重启保留进度，忘记成功时影子空间也已清除该事实。"""
    import psycopg
    from psycopg import sql
    target_facts: list[str]=[]
    def provider(request: httpx.Request) -> httpx.Response:
        """记录实际发往目标服务的事实，供确认已完成步骤没有重复请求。"""
        payload=json.loads(request.content)
        if request.url.host=='new-vector.example' and payload.get('input')!=['连接验证']:
            target_facts.extend(payload['input'])
        return rebuild_provider(request)
    transport=httpx.MockTransport(provider)
    identifier=str(uuid4())
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        for fact in ['我喜欢茶','我住在杭州']:
            client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：'+fact})
        entries=client.get('/api/memories').json()
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        assert client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True}).status_code==202
        paused=wait_rebuild(client,identifier,'paused')
        assert paused['completed']==0 and target_facts==[]
        paused=approve_step(client,identifier,paused)
        assert paused['completed']==0 and target_facts==[]
        paused=approve_step(client,identifier,paused)
        assert paused['completed']==1 and len(target_facts)==1
        # 维护门槛必须在接受更正版本之前生效，旧事实仍完整有效。
        entry=entries[0]
        blocked=client.put(f"/api/memories/{entry['id']}",json={'fact':'我喜欢咖啡','expected_version':entry['version']})
        assert blocked.status_code==409 and '重建' in blocked.text
        assert {item['id']:item['version'] for item in client.get('/api/memories').json()}=={item['id']:item['version'] for item in entries}
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        assert wait_rebuild(client,identifier,'paused')['pause']==paused['pause']
        deleted=next(item for item in entries if item['fact']==target_facts[0])
        assert client.delete(f"/api/memories/{deleted['id']}").status_code==200
        assert len(client.get('/api/memories').json())==1
        # 真适配契约：成功回执之前，登记的每个向量空间及历史均不可残留该ID。
        with psycopg.connect(database_url) as connection:
            spaces=connection.execute("SELECT collection_name FROM embedding_spaces WHERE status<>'removed'").fetchall()
            for (name,) in spaces:
                assert connection.execute(sql.SQL('SELECT id FROM {} WHERE id=%s').format(sql.Identifier(name)),(deleted['memory_id'],)).fetchall()==[]
            assert connection.execute('SELECT id FROM mem0_history WHERE memory_id=%s',(deleted['memory_id'],)).fetchall()==[]
        done=approve_step(client,identifier,paused)
        assert done['status']=='complete' and done['excluded']==1
        assert len(target_facts)==2 and len(set(target_facts))==2
        assert [item['fact'] for item in client.get('/api/memories').json()]==[item['fact'] for item in entries if item['id']!=deleted['id']]


def test_failed_target_can_cancel_without_losing_old_memory(database_url: str, tmp_path: Path) -> None:
    """错误目标维度诚实失败，取消清目标并恢复原活动配置，已有事实保持可查。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(rebuild_provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢简短回答'})
        entry=client.get('/api/memories').json()[0]
        identifier=str(uuid4())
        wrong={**target(),'dimensions':5}
        assert client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':wrong,'confirmed':True}).status_code==202
        failed=wait_rebuild(client,identifier,'failed')
        assert '维度' in failed['error'] and failed['queryable'] is False
        assert client.get('/api/settings/embedding').json()['model']=='embed'
        assert client.get('/api/memories').json()[0]['memory_id']==entry['memory_id']
        new=client.post('/api/conversations',json={}).json()['id']
        reply=client.post(f'/api/conversations/{new}/messages',json={'content':'回答应采用什么风格？'})
        assert reply.status_code==200 and reply.json()['messages'][-1]['memory_recall']['status']=='unavailable'
        assert client.post(f'/api/settings/embedding/rebuilds/{identifier}/cancel').status_code==202
        cancelled=wait_rebuild(client,identifier,'cancelled')
        assert cancelled['queryable'] is True
        assert client.post(f'/api/settings/embedding/rebuilds/{identifier}/resume').json()['status']=='cancelled'
        reply=client.post(f'/api/conversations/{new}/messages',json={'content':'回答应采用什么风格？'})
        assert reply.json()['messages'][-1]['memory_recall']['count']==1


def test_kept_source_deleted_memory_rebuild_preserves_sdk_history_and_times(database_url: str, tmp_path: Path) -> None:
    """来源已删但明确保留的有效事实参加重建，未确认候选和已删事实不参加。"""
    import psycopg
    from psycopg import sql
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(rebuild_provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢茶'})
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我住在杭州'})
        entries=client.get('/api/memories').json()
        keep=entries[0]
        preview=client.get(f'/api/conversations/{cid}/deletion-preview').json()
        deleted=client.request('DELETE',f'/api/conversations/{cid}',json={'preview_revision':preview['revision'],'keep_memory_ids':[keep['id']]})
        assert deleted.status_code==200,deleted.text
        retained=client.get('/api/memories').json()[0]
        assert retained['source_deleted'] and retained['message_id'] is None
        with psycopg.connect(database_url) as connection:
            stored=connection.execute('SELECT payload FROM personal_memories WHERE id=%s',(keep['memory_id'],)).fetchone()
            assert stored is not None
            before=stored[0]
            history=connection.execute('SELECT * FROM mem0_history WHERE memory_id=%s ORDER BY seq',(keep['memory_id'],)).fetchall()
        identifier=str(uuid4())
        client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True})
        assert wait_rebuild(client,identifier,'complete')['total']==1
        assert client.get('/api/memories').json()[0]['fact']==keep['fact']
        with psycopg.connect(database_url) as connection:
            current=connection.execute("SELECT collection_name FROM embedding_spaces WHERE status='active'").fetchone()
            assert current is not None
            name=current[0]
            rebuilt=connection.execute(sql.SQL('SELECT payload FROM {} WHERE id=%s').format(sql.Identifier(name)),(keep['memory_id'],)).fetchone()
            assert rebuilt is not None
            after=rebuilt[0]
            assert after['created_at']==before['created_at']
            assert after.get('updated_at')==before.get('updated_at')
            assert after['source_deleted'] and 'source_message_id' not in after and 'source_conversation_id' not in after
            assert connection.execute('SELECT * FROM mem0_history WHERE memory_id=%s ORDER BY seq',(keep['memory_id'],)).fetchall()==history


def test_partial_target_write_and_publishing_failure_recover_without_reembedding(database_url: str, tmp_path: Path) -> None:
    """SDK 已提交而步骤提交失败时先对账；文件已切而 DB 未切时保持维护并重启恢复。"""
    import psycopg
    target_inputs: list[str]=[]
    def provider(request: httpx.Request) -> httpx.Response:
        """仅计数目标服务实际接收的输入，验证恢复不会重复发送已持久化事实。"""
        payload=json.loads(request.content)
        if request.url.host=='new-vector.example':
            target_inputs.extend(payload['input'])
        return rebuild_provider(request)
    transport=httpx.MockTransport(provider)
    identifier=str(uuid4())
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢茶'})
        with psycopg.connect(database_url) as connection:
            # 阻止目标向量完成检查点提交，验证重启后核对真实落库而不重复嵌入。
            connection.execute("CREATE FUNCTION reject_rebuild_progress() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action_id IS NOT NULL AND NEW.status='complete' THEN RAISE EXCEPTION 'progress unavailable'; END IF; RETURN NEW; END $$")
            connection.execute('CREATE TRIGGER reject_rebuild_progress BEFORE UPDATE ON embedding_rebuild_steps FOR EACH ROW EXECUTE FUNCTION reject_rebuild_progress()')
        client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True})
        assert wait_rebuild(client,identifier,'failed')['completed']==0
        assert target_inputs==['连接验证','我喜欢茶']
        with psycopg.connect(database_url) as connection:
            connection.execute('DROP TRIGGER reject_rebuild_progress ON embedding_rebuild_steps')
            # 阻止目标空间发布，验证未完成切换不能提前允许查询。
            connection.execute("CREATE FUNCTION reject_space_publish() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.model='new-embed' THEN RAISE EXCEPTION 'publish unavailable'; END IF; RETURN NEW; END $$")
            connection.execute('CREATE TRIGGER reject_space_publish BEFORE UPDATE ON embedding_indexes FOR EACH ROW EXECUTE FUNCTION reject_space_publish()')
        client.post(f'/api/settings/embedding/rebuilds/{identifier}/resume')
        deadline=monotonic()+10
        while monotonic()<deadline:
            failed=client.get(f'/api/settings/embedding/rebuilds/{identifier}').json()
            if failed['status']=='failed' and failed['phase']=='publishing':
                break
            sleep(.02)
        assert failed['phase']=='publishing' and failed['queryable'] is False
        assert target_inputs==['连接验证','我喜欢茶']
        assert client.get('/api/memories').json()[0]['fact']=='我喜欢茶'
        assert client.post(f'/api/settings/embedding/rebuilds/{identifier}/cancel').status_code==409
    with psycopg.connect(database_url) as connection:
        connection.execute('DROP TRIGGER reject_space_publish ON embedding_indexes')
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        client.post(f'/api/settings/embedding/rebuilds/{identifier}/resume')
        assert wait_rebuild(client,identifier,'complete')['queryable'] is True
        assert target_inputs==['连接验证','我喜欢茶']
        assert client.get('/api/settings/embedding').json()['model']=='new-embed'


def test_inflight_correction_finishes_before_snapshot_and_new_writes_fail_fast(database_url: str, tmp_path: Path) -> None:
    """维护等待已在途更正完整发布，新的更正不排在慢网络后面且不接受版本。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered,release=Event(),Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """只延迟旧空间的明确更正嵌入，重建目标响应保持正常。"""
        payload=json.loads(request.content)
        if request.url.host=='vector.example' and payload.get('input')==['我喜欢咖啡']:
            entered.set()
            assert release.wait(10)
        return rebuild_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢茶'})
        entry=client.get('/api/memories').json()[0]
        identifier=str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:
            changing=pool.submit(client.put,f"/api/memories/{entry['id']}",json={'fact':'我喜欢咖啡','expected_version':1})
            assert entered.wait(5)
            try:
                assert client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True}).status_code==202
                # 通过第二条 HTTP 验证维护门槛即时返回，而非占住用户请求等待旧云请求。
                blocked=pool.submit(client.put,f"/api/memories/{entry['id']}",json={'fact':'我喜欢果汁','expected_version':2})
                response=blocked.result(timeout=2)
                assert response.status_code==409 and '重建' in response.text
            finally:
                release.set()
            assert changing.result().status_code==200
        done=wait_rebuild(client,identifier,'complete')
        assert done['completed']==1
        final=client.get('/api/memories').json()[0]
        assert final['memory_id']==entry['memory_id'] and final['version']==2 and final['fact']=='我喜欢咖啡'


def test_publish_checks_actual_target_and_repairs_missing_committed_row(database_url: str, tmp_path: Path) -> None:
    """完成计数不能代替真实索引完整性，目标行丢失时拒绝发布并只修复缺失事实。"""
    import psycopg
    from psycopg import sql
    from threading import Event
    entered,release=Event(),Event()
    facts: list[str]=[]
    def provider(request: httpx.Request) -> httpx.Response:
        """在第二条目标事实处等待，允许数据库边界模拟第一条目标向量丢失。"""
        payload=json.loads(request.content)
        if request.url.host=='new-vector.example' and payload['input']!=['连接验证']:
            facts.extend(payload['input'])
            if len(facts)==2:
                entered.set()
                assert release.wait(10)
        return rebuild_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        for fact in ['我喜欢茶','我住在杭州']:
            client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：'+fact})
        identifier=str(uuid4())
        client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True})
        assert entered.wait(5)
        try:
            with psycopg.connect(database_url) as connection:
                space=connection.execute("SELECT collection_name FROM embedding_spaces WHERE status='target'").fetchone()
                assert space is not None
                name=space[0]
                connection.execute(sql.SQL('DELETE FROM {}').format(sql.Identifier(name)))
        finally:
            release.set()
        failed=wait_rebuild(client,identifier,'failed')
        assert failed['queryable'] is False
        assert client.get('/api/settings/embedding').json()['model']=='embed'
        client.post(f'/api/settings/embedding/rebuilds/{identifier}/resume')
        assert wait_rebuild(client,identifier,'complete')['completed']==2
        assert len(facts)==3 and facts[0]==facts[2]


def test_delete_during_target_embedding_never_revives_fact(database_url: str, tmp_path: Path) -> None:
    """目标嵌入等待时忘记立即停用，迟到响应计费但不得写回任何登记空间。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import psycopg
    from psycopg import sql
    entered,release=Event(),Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """只延迟目标事实嵌入，允许从公开删除接口接受删除墓碑。"""
        payload=json.loads(request.content)
        if request.url.host=='new-vector.example' and payload['input']==['我喜欢茶']:
            entered.set()
            assert release.wait(10)
        return rebuild_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢茶'})
        entry=client.get('/api/memories').json()[0]
        identifier=str(uuid4())
        client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True})
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            deleting=pool.submit(client.delete,f"/api/memories/{entry['id']}")
            try:
                deadline=monotonic()+2
                while monotonic()<deadline:
                    rows=client.get('/api/memories').json()
                    if rows and rows[0]['status']=='deleting':
                        break
                    sleep(.02)
                assert rows[0]['status']=='deleting' and rows[0]['fact']==''
            finally:
                release.set()
            assert deleting.result(timeout=5).status_code==200
        wait_rebuild(client,identifier,'failed')
        assert client.get('/api/memories').json()==[]
        with psycopg.connect(database_url) as connection:
            for (name,) in connection.execute("SELECT collection_name FROM embedding_spaces WHERE status<>'removed'").fetchall():
                assert connection.execute(sql.SQL('SELECT id FROM {}').format(sql.Identifier(name))).fetchall()==[]
            assert connection.execute('SELECT old_memory,new_memory FROM mem0_history').fetchall()==[]
        client.post(f'/api/settings/embedding/rebuilds/{identifier}/resume')
        assert wait_rebuild(client,identifier,'complete')['excluded']==1
        assert client.get('/api/memories').json()==[]


def test_source_deletion_can_stop_inflight_write_while_rebuild_waits(database_url: str, tmp_path: Path) -> None:
    """重建等待旧嵌入时，来源预览和删除接受不被独占锁排队阻塞。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered,release=Event(),Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """只让正在更正的旧嵌入等待，来源删除应能在返回前停用它。"""
        payload=json.loads(request.content)
        if request.url.host=='vector.example' and payload.get('input')==['我喜欢咖啡']:
            entered.set()
            assert release.wait(10)
        return rebuild_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢茶'})
        entry=client.get('/api/memories').json()[0]
        identifier=str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as pool:
            changing=pool.submit(client.put,f"/api/memories/{entry['id']}",json={'fact':'我喜欢咖啡','expected_version':1})
            assert entered.wait(5)
            try:
                client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True})
                preview=pool.submit(client.get,f'/api/conversations/{cid}/deletion-preview').result(timeout=2)
                assert preview.status_code==200
                deleting=pool.submit(client.request,'DELETE',f'/api/conversations/{cid}',json={'preview_revision':preview.json()['revision'],'keep_memory_ids':[]})
                deadline=monotonic()+2
                while monotonic()<deadline and client.get(f'/api/conversations/{cid}').status_code!=404:
                    sleep(.02)
                assert client.get(f'/api/conversations/{cid}').status_code==404
            finally:
                release.set()
            assert changing.result().status_code==409
            assert deleting.result(timeout=5).status_code==200
        assert wait_rebuild(client,identifier,'complete')['total']==0
        assert client.get('/api/memories').json()==[]


def test_cancel_inflight_target_keeps_old_index_and_does_not_send_next_fact(database_url: str, tmp_path: Path) -> None:
    """取消在途重建先撤回后续步骤，已发生调用计量，目标清理后旧索引仍有效。"""
    from threading import Event
    entered,release=Event(),Event()
    facts: list[str]=[]
    def provider(request: httpx.Request) -> httpx.Response:
        """目标第一条事实等待取消，第二条不得再发送。"""
        payload=json.loads(request.content)
        if request.url.host=='new-vector.example' and payload['input']!=['连接验证']:
            facts.extend(payload['input'])
            entered.set()
            assert release.wait(10)
        return rebuild_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        for fact in ['我喜欢茶','我住在杭州']:
            client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：'+fact})
        before=client.get('/api/memories').json()
        identifier=str(uuid4())
        client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True})
        assert entered.wait(5)
        try:
            cancelled=client.post(f'/api/settings/embedding/rebuilds/{identifier}/cancel')
            assert cancelled.status_code==202 and cancelled.json()['status']=='cancelling'
        finally:
            release.set()
        assert wait_rebuild(client,identifier,'cancelled')['queryable'] is True
        assert len(facts)==1
        assert client.get('/api/memories').json()==before
        assert client.get('/api/settings/embedding').json()['model']=='embed'
        calls=[row for row in client.get('/api/usage').json() if row['purpose']=='embedding_rebuild']
        assert len(calls)==2 and all(row['prompt_tokens']==7 for row in calls)


def test_model_only_then_dimension_only_changes_and_key_rotation(database_url: str, tmp_path: Path) -> None:
    """同维度换模型和同模型换维度均重建，同空间换密钥只要求重新验证而不重建。"""
    dimensions=3
    def provider(request: httpx.Request) -> httpx.Response:
        """外部服务在明确切换维度后返回新尺寸，不能由请求内部实现自行推断。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200,json={'model':payload['model'],'data':[{'index':i,'embedding':[1.0]+[0.0]*(dimensions-1)} for i,_ in enumerate(payload['input'])],'usage':{'prompt_tokens':7,'total_tokens':7}})
        return memory_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢茶'})
        before=client.get('/api/memories').json()
        for dimensions in (3,4):
            body={'base_url':'https://vector.example/v1','model':'replacement','dimensions':dimensions,'api_key':'test'}
            identifier=str(uuid4())
            assert client.post('/api/settings/embedding/rebuild-impact',json=body).json()['requires_rebuild'] is True
            assert client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':body,'confirmed':True}).status_code==202
            assert wait_rebuild(client,identifier,'complete')['completed']==1
            assert client.get('/api/memories').json()==before
        count=len(client.get('/api/usage').json())
        body['api_key']='rotated-test-key'
        assert client.post('/api/settings/embedding/rebuild-impact',json=body).json()['requires_rebuild'] is False
        assert client.put('/api/settings/embedding',json=body).status_code==200
        assert len(client.get('/api/usage').json())==count
        assert client.get('/api/settings/capabilities').json()['embedding']['status']=='unverified'


def test_unconfirmed_candidate_stays_pending_and_can_confirm_after_rebuild(database_url: str, tmp_path: Path) -> None:
    """候选不作为重建来源，维护期间点击确认不改变批准状态，完成后仍可单独确认。"""
    from test_memory_candidates import candidate_provider
    def provider(request: httpx.Request) -> httpx.Response:
        """仅当前消息发现返回候选，所有嵌入仍由重建测试的外部协议提供。"""
        if request.url.path.endswith('/embeddings'):
            return rebuild_provider(request)
        return candidate_provider(request)
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢茶'})
        answer=client.post(f'/api/conversations/{cid}/messages',json={'content':'我喜欢简洁的中文回答'}).json()['messages'][-1]
        candidate=answer['memory_candidates'][0]
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        identifier=str(uuid4())
        client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True})
        assert wait_rebuild(client,identifier,'paused')['total']==1
        blocked=client.post(f"/api/memory-candidates/{candidate['id']}/confirm")
        assert blocked.status_code==409 and '重建' in blocked.text
        restored=client.get(f'/api/conversations/{cid}').json()['messages'][-1]['memory_candidates'][0]
        assert restored['status']=='pending' and restored['approved_at'] is None
        client.put('/api/settings/budget',json={'monthly_limit':None})
        client.post(f'/api/settings/embedding/rebuilds/{identifier}/resume')
        assert wait_rebuild(client,identifier,'complete')['total']==1
        assert len(client.get('/api/memories').json())==1
        assert client.post(f"/api/memory-candidates/{candidate['id']}/confirm").status_code==200
        assert len(client.get('/api/memories').json())==2


def test_unfinished_correction_is_not_stranded_by_dropping_its_old_space(database_url: str, tmp_path: Path) -> None:
    """已有暂停更正必须先恢复或明确忘记，重建不能丢掉它重试所需的原正式 ID。"""
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(rebuild_provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢茶'})
        entry=client.get('/api/memories').json()[0]
        client.put('/api/settings/budget',json={'monthly_limit':'0'})
        changed=client.put(f"/api/memories/{entry['id']}",json={'fact':'我喜欢咖啡','expected_version':1})
        assert changed.status_code==402
        client.put('/api/settings/budget',json={'monthly_limit':None})
        identifier=str(uuid4())
        client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True})
        failed=wait_rebuild(client,identifier,'failed')
        assert '未完成' in failed['error'] and failed['queryable'] is False
        assert client.get('/api/settings/embedding').json()['model']=='embed'
        client.post(f'/api/settings/embedding/rebuilds/{identifier}/cancel')
        wait_rebuild(client,identifier,'cancelled')
        update=client.get('/api/memories').json()[0]['updates'][0]
        assert client.post(f"/api/memory-updates/{update['id']}/retry").status_code==200
        assert client.get('/api/memories').json()[0]['memory_id']==entry['memory_id']
        next_id=str(uuid4())
        client.post('/api/settings/embedding/rebuilds',json={'id':next_id,'target':target(),'confirmed':True})
        assert wait_rebuild(client,next_id,'complete')['completed']==1
        assert client.get('/api/memories').json()[0]['fact']=='我喜欢咖啡'


def test_retired_cleanup_failure_keeps_new_queries_and_delete_cleans_both_spaces(database_url: str, tmp_path: Path) -> None:
    """旧表清理失败不回退已发布空间；忘记仍清旧新副本，恢复只做本地清理。"""
    import psycopg
    from psycopg import sql
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(rebuild_provider)),base_url='http://127.0.0.1:8000') as client:
        cid=configure_memory(client)
        client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：我喜欢茶'})
        entry=client.get('/api/memories').json()[0]
        with psycopg.connect(database_url) as connection:
            connection.execute('CREATE VIEW protected_old_space AS SELECT id FROM personal_memories')
        identifier=str(uuid4())
        client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':target(),'confirmed':True})
        failed=wait_rebuild(client,identifier,'failed')
        assert failed['phase']=='cleanup' and failed['queryable'] is True
        assert client.get('/api/settings/embedding').json()['model']=='new-embed'
        assert client.delete(f"/api/memories/{entry['id']}").status_code==200
        with psycopg.connect(database_url) as connection:
            for (name,) in connection.execute("SELECT collection_name FROM embedding_spaces WHERE status<>'removed'").fetchall():
                assert connection.execute(sql.SQL('SELECT id FROM {}').format(sql.Identifier(name))).fetchall()==[]
            assert connection.execute('SELECT id FROM mem0_history').fetchall()==[]
            connection.execute('DROP VIEW protected_old_space')
        count=len(client.get('/api/usage').json())
        client.post(f'/api/settings/embedding/rebuilds/{identifier}/resume')
        assert wait_rebuild(client,identifier,'complete')['queryable'] is True
        assert len(client.get('/api/usage').json())==count
