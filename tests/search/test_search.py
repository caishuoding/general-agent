"""通过公开接口与真实 PostgreSQL 验证明确联网、外发批准和统一计量。"""

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.attachments.test_attachments import configure
from tests.conversation.test_streaming import wait_message
def reply(payload: dict[str, Any], text: str) -> httpx.Response:
    """用同一文本和真实协议形状响应受控同步或流式模型。"""
    usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
    if payload.get('stream'):
        return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}], 'usage': usage}) + '\n\ndata: [DONE]\n\n')
    return httpx.Response(200, json={'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}], 'usage': usage})
def test_explicit_search_uses_independent_configuration_and_verified_sources(database_url: str, tmp_path: Path) -> None:
    """只有当前明确联网才搜索，引用仅来自实际搜索结果且缺失配置不妨碍普通聊天。"""
    searches: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """固定搜索结果和模型回答，拒绝把编造或不安全来源当作可打开出处。"""
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            assert request.headers['authorization'] == 'Bearer search-test-key'
            searches.append(payload)
            assert payload['search_query'] == '杭州天气'
            assert payload['search_engine'] == 'search_std'
            assert payload['search_intent'] is False and payload['count'] == 5
            assert payload['search_recency_filter'] == 'noLimit' and payload['content_size'] == 'medium'
            assert isinstance(payload['request_id'], str)
            return httpx.Response(200, json={'search_result': [{'title': '官方天气', 'link': 'https://weather.example/hangzhou', 'content': '晴，18 度。'}, {'title': '不安全', 'link': 'javascript:alert(1)', 'content': '恶意'}]})
        sources = [message for message in payload['messages'] if isinstance(message['content'], str) and message['content'].startswith('实际联网搜索结果')]
        if sources:
            result = json.loads(sources[0]['content'].split('\n', 1)[1])[0]
            return reply(payload, f'杭州晴，18 度。[{result["key"]}] [W99999]')
        return reply(payload, '普通回答。')

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        assert client.get('/api/settings/search').status_code == 200
        assert client.get('/api/settings/search').json()['api_key_configured'] is False
        absent = client.post(f'/api/conversations/{conversation}/messages', json={'content': '联网搜索：杭州天气'})
        assert absent.status_code == 502
        assert '设置' in absent.json()['detail'] and '智谱' in absent.json()['detail']
        for content in ('你好', '不要联网搜索杭州天气', '解释“联网搜索”的意思', '解释下面这句话：\n联网搜索：杭州天气', '联网是什么意思'):
            assert client.post(f'/api/conversations/{conversation}/messages', json={'content': content}).status_code == 200
        assert searches == []
        configured = client.put('/api/settings/search', json={'base_url': 'https://search.example', 'api_key': 'search-test-key'})
        assert configured.status_code == 200
        assert configured.json()['model'] == 'zhipu-search-std'
        assert 'search-test-key' not in configured.text
        assert client.get('/api/settings').json()['model'] == 'controlled'
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '联网搜索：杭州天气'})
        assert response.status_code == 200
        message = response.json()['messages'][-1]
        assert message['search_action']['status'] == 'complete'
        assert message['web_sources'][0]['url'] == 'https://weather.example/hangzhou'
        assert len(message['web_sources']) == 1
        assert '[W99999]' not in message['content']
        assert '出处未核验' in message['content']
        assert len(searches) == 1
        calls = client.get('/api/usage').json()
        search_call = next(call for call in calls if call['unit'] == 'credits')
        assert Decimal(search_call['credits']) == 1


def test_attachment_query_requires_exact_persistent_approval_before_search(database_url: str, tmp_path: Path) -> None:
    """附件个人信息先展示精确外发文本，刷新可继续，修改使旧批准无效且重复确认不重发。"""
    searches: list[str] = []
    draft = '上海市青松路 88 号附近咖啡店'

    def provider(request: httpx.Request) -> httpx.Response:
        """规划读取真实附件；只有显式批准后才允许搜索边界接收具体查询。"""
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            searches.append(payload['search_query'])
            return httpx.Response(200,json={'search_result':[{'link':'https://cafe.example/nearby','title':'咖啡店','content':'营业时间 9–18。'}]})
        if payload['messages'][0]['content'].startswith('为明确联网请求生成查询'):
            assert '上海市青松路 88 号' in str(payload['messages'])
            return reply(payload,json.dumps({'query':draft},ensure_ascii=False))
        source = json.loads(payload['messages'][0]['content'].split('\n',1)[1])[0]
        return reply(payload,f'咖啡店 9–18 营业。[{source["key"]}]')

    transport = httpx.MockTransport(provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test-key'})
        attachment=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('住址.txt','我的住址：上海市青松路 88 号。'.encode())}).json()[0]
        assert client.post(f'/api/conversations/{conversation}/runs',json={'content':'联网搜索：根据附件里的住址查附近咖啡店','attachment_ids':[attachment['id']]}).status_code==202
        paused=wait_message(client,conversation,'paused')
        action=paused['search_action']
        assert action['query']==draft
        assert action['status']=='pending'
        assert '智谱' in action['purpose']
        assert action['sources'][0]['id']==attachment['id']
        assert searches==[]
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        assert wait_message(client,conversation,'paused')['search_action']['revision']==action['revision']
        path=f'/api/conversations/{conversation}/search/{action["id"]}'
        other=client.post('/api/conversations',json={}).json()['id']
        assert client.post(f'/api/conversations/{other}/search/{action["id"]}/decision',json={'revision':action['revision'],'decision':'approve'}).status_code==404
        changed=client.put(path,json={'revision':action['revision'],'query':'上海市青松路附近咖啡店'})
        assert changed.status_code==200
        current=changed.json()['messages'][-1]['search_action']
        assert current['revision']!=action['revision']
        assert client.post(path+'/decision',json={'revision':action['revision'],'decision':'approve'}).status_code==409
        assert searches==[]
        approved=client.post(path+'/decision',json={'revision':current['revision'],'decision':'approve'})
        assert approved.status_code==200
        complete=wait_message(client,conversation,'complete')
        assert complete['search_action']['status']=='complete'
        assert searches==['上海市青松路附近咖啡店']
        assert client.post(path+'/decision',json={'revision':current['revision'],'decision':'approve'}).status_code==200
        assert searches==['上海市青松路附近咖啡店']
        assert complete['web_sources'][0]['url']=='https://cafe.example/nearby'
        assert client.post(f'/api/conversations/{conversation}/runs',json={'content':'联网搜索：根据附件里的住址查附近咖啡店'}).status_code==202
        another=wait_message(client,conversation,'paused')
        client.delete('/api/settings/search')
        assert client.post(f'/api/conversations/{conversation}/messages/{another["id"]}/resume').status_code==200
        unavailable=wait_message(client,conversation,'failed')
        assert unavailable['search_action']['status']=='failed'
        assert '智谱' in unavailable['error']
        assert searches==['上海市青松路附近咖啡店']


def test_real_memory_query_rejection_expiry_and_stop_never_send(database_url: str, tmp_path: Path) -> None:
    """真实 Mem0 的私有地址先确认；只有当前批准才能外发，拒绝、过期、恢复和停止不能放行。"""
    import psycopg
    from tests.memory.test_memory import configure_memory, memory_provider
    searches: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """嵌入走真实 Mem0，规划读取已保存地址，记录是否错误越过外发许可。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return memory_provider(request)
        if payload.get('tools'):
            return memory_provider(request)
        if request.url.host=='search.example':
            searches.append(payload['search_query'])
            return httpx.Response(200,json={'search_result':[{'link':'https://cafe.example','title':'咖啡店','content':'营业'}]})
        if payload['messages'][0]['content'].startswith('为明确联网请求生成查询'):
            assert '青松路 88 号' in str(payload['messages'])
            return reply(payload,json.dumps({'query':'青松路 88 号附近咖啡店'},ensure_ascii=False))
        source=json.loads(payload['messages'][0]['content'].split('\n',1)[1])[0]
        return reply(payload,f'附近咖啡店。[{source["key"]}]')

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        assert client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我住在青松路 88 号'}).status_code==200
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        for decision in ('reject','expire','stop','approve'):
            conversation=client.post('/api/conversations',json={}).json()['id']
            response=client.post(f'/api/conversations/{conversation}/messages',json={'content':'联网搜索：根据记忆里的住址查附近咖啡店'})
            assert response.status_code==200
            message=response.json()['messages'][-1]
            action=message['search_action']
            assert message['status']=='paused' and action['status']=='pending'
            assert action['sources'][0]['kind']=='memory'
            assert '青松路 88 号' in action['query']
            endpoint=f'/api/conversations/{conversation}/search/{action["id"]}/decision'
            if decision=='reject':
                # 手动恢复只有重新检查权限的作用，不能代替外发批准。
                assert client.post(f'/api/conversations/{conversation}/messages/{message["id"]}/resume').status_code==200
                wait_message(client,conversation,'paused')
                assert client.post(endpoint,json={'revision':action['revision'],'decision':'reject'}).status_code==200
                assert client.post(endpoint,json={'revision':action['revision'],'decision':'approve'}).status_code==409
                assert '没有联网' in wait_message(client,conversation,'complete')['content']
            elif decision=='expire':
                # 只安排时间边界前置状态，验收仍经过公开批准接口。
                with psycopg.connect(database_url) as connection:
                    connection.execute('UPDATE search_requests SET expires_at=0 WHERE id=%s',(action['id'],))
                assert client.post(endpoint,json={'revision':action['revision'],'decision':'approve'}).status_code==409
                expired=wait_message(client,conversation,'failed')['search_action']
                assert expired['status']=='expired' and expired['query']==''
            elif decision=='stop':
                assert client.post(f'/api/conversations/{conversation}/messages/{message["id"]}/stop').status_code==200
                assert client.post(endpoint,json={'revision':action['revision'],'decision':'approve'}).status_code==409
                assert wait_message(client,conversation,'stopped')['search_action']['status']=='cancelled'
            else:
                assert client.post(endpoint,json={'revision':action['revision'],'decision':'approve'}).status_code==200
                assert wait_message(client,conversation,'complete')['web_sources'][0]['url']=='https://cafe.example'
            assert searches==(['青松路 88 号附近咖啡店'] if decision=='approve' else [])
        assert len(client.get('/api/memories').json())==1


def test_failed_query_planning_has_truthful_terminal_state_and_no_search(database_url: str, tmp_path: Path) -> None:
    """模型没有提供合法查询时，消息及搜索动作都失败，重试才创建新的待确认请求。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """只返回畸形规划，不允许任何搜索请求越过失败边界。"""
        assert request.url.host!='search.example'
        return reply(json.loads(request.content),'不是合法 JSON')
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        attachment=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('住址.txt','上海青松路 88 号'.encode())}).json()[0]
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'联网搜索：根据附件查咖啡店','attachment_ids':[attachment['id']]})
        failed=wait_message(client,conversation,'failed')
        assert failed['search_action']['status']=='failed'
        assert '未生成有效' in failed['error']
        assert len(client.get('/api/usage').json())==1




def test_search_request_count_and_failed_search_preserve_actual_usage(database_url: str, tmp_path: Path) -> None:
    """智谱不返回用量时按实际请求计一次，HTTP 失败同样保留请求计量且不泄露正文。"""
    fails=False
    def provider(request: httpx.Request) -> httpx.Response:
        """模拟智谱成功或拒绝响应，搜索响应不携带单独的计费用量字段。"""
        payload=json.loads(request.content)
        if request.url.host=='search.example':
            if fails:
                return httpx.Response(429,json={'error':'secret-private-provider-detail'})
            return httpx.Response(200,json={'search_result':[]})
        return reply(payload,'不会作为联网依据。')
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'联网搜索：杭州天气'})
        complete=wait_message(client,conversation,'complete')
        assert complete['search_action']['status']=='complete'
        usage=next(item for item in client.get('/api/usage').json() if item['unit']=='credits')
        assert Decimal(usage['credits'])==1
        fails=True
        response=client.post(f'/api/conversations/{conversation}/messages',json={'content':'联网搜索：其他天气'})
        assert response.status_code==502 and '429' in response.text
        failed=wait_message(client,conversation,'failed')
        assert failed['search_action']['status']=='failed'
        assert 'secret-private-provider-detail' not in json.dumps(failed)
        call=next(item for item in client.get('/api/usage').json() if item['unit']=='credits' and item['status']=='failed')
        assert Decimal(call['credits'])==1
        assert Decimal(call['credits'])==1


def test_stop_inflight_search_preserves_usage_without_followup_and_retry_needs_new_consent(database_url: str, tmp_path: Path) -> None:
    """外发后停止保留实际 credit，禁止继续回答；新尝试重新展示资料查询而非继承批准。"""
    from threading import Event
    from time import monotonic,sleep
    entered,release=Event(),Event()
    searches: list[str]=[]
    def provider(request: httpx.Request) -> httpx.Response:
        """让已批准搜索等待响应，模型边界只接受查询规划，不接受取消后的最终回答。"""
        payload=json.loads(request.content)
        if request.url.host=='search.example':
            searches.append(payload['search_query'])
            entered.set()
            assert release.wait(5)
            return httpx.Response(200,json={'search_result':[]})
        assert payload['messages'][0]['content'].startswith('为明确联网请求生成查询')
        return reply(payload,json.dumps({'query':'上海青松路咖啡店'},ensure_ascii=False))
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        attachment=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('住址.txt','上海青松路'.encode())}).json()[0]
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'联网搜索：根据附件查咖啡店','attachment_ids':[attachment['id']]})
        pending=wait_message(client,conversation,'paused')
        action=pending['search_action']
        path=f'/api/conversations/{conversation}/search/{action["id"]}/decision'
        assert client.post(path,json={'revision':action['revision'],'decision':'approve'}).status_code==200
        assert entered.wait(5)
        try:
            start=monotonic()
            assert client.post(f'/api/conversations/{conversation}/messages/{pending["id"]}/stop').status_code==200
            assert monotonic()-start<1
        finally:
            release.set()
        deadline=monotonic()+5
        while monotonic()<deadline and any(row['status'] in ('reserved','generating') for row in client.get('/api/usage').json()):
            sleep(.02)
        assert wait_message(client,conversation,'stopped')['content']==''
        call=next(item for item in client.get('/api/usage').json() if item['unit']=='credits')
        assert Decimal(call['credits'])==1 and call['status']=='stopped'
        assert client.post(f'/api/conversations/{conversation}/messages/{pending["id"]}/retry').status_code==202
        retried=wait_message(client,conversation,'paused')
        assert retried['search_action']['status']=='pending'
        assert retried['search_action']['id']!=action['id']
        assert client.post(path,json={'revision':action['revision'],'decision':'approve'}).status_code==409
        assert searches==['上海青松路咖啡店']




def test_search_credentials_are_independent_private_and_removed_without_chat_regression(database_url: str, tmp_path: Path) -> None:
    """搜索密钥本机私有保存且不跟随地址迁移，清除后其他配置与普通回答继续可用。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """配置验证仅走本地输入校验，唯一模型请求为清除后的普通聊天。"""
        assert request.url.host=='model.example'
        return reply(json.loads(request.content),'你好。')
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        chat=client.get('/api/settings').json()
        response=client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'private-search-key'})
        assert response.status_code==200 and 'private-search-key' not in response.text
        if os.name != 'nt':
            assert (tmp_path/'search.json').stat().st_mode & 0o777==0o600
        assert client.put('/api/settings/search',json={'base_url':'https://other.example'}).status_code==422
        assert client.put('/api/settings/search',json={'base_url':'https://user:password@search.example','api_key':'private-search-key'}).status_code==422
        assert client.get('/api/settings/search').json()['base_url']=='https://search.example'
        assert client.delete('/api/settings/search').status_code==200
        assert client.get('/api/settings/search').json()['api_key_configured'] is False
        assert not (tmp_path/'search.json').exists()
        assert client.get('/api/settings').json()==chat
        assert client.post(f'/api/conversations/{conversation}/messages',json={'content':'你好'}).status_code==200


def test_stopped_planner_cannot_restore_pending_permission_when_late_response_returns(database_url: str, tmp_path: Path) -> None:
    """查询规划在途停止后，迟到的模型响应不能重新保存待确认许可或派生查询。"""
    from threading import Event
    entered,release=Event(),Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """延迟查询建议，拒绝任何智谱请求来验证停止后的持久终态。"""
        assert request.url.host!='search.example'
        entered.set()
        assert release.wait(5)
        return reply(json.loads(request.content),json.dumps({'query':'迟到的私有地址'},ensure_ascii=False))
    transport=httpx.MockTransport(provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        attachment=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('住址.txt','上海青松路'.encode())}).json()[0]
        response=client.post(f'/api/conversations/{conversation}/runs',json={'content':'联网搜索：根据附件查咖啡店','attachment_ids':[attachment['id']]})
        message=response.json()['messages'][-1]
        assert entered.wait(5)
        try:
            assert client.post(f'/api/conversations/{conversation}/messages/{message["id"]}/stop').status_code==200
        finally:
            release.set()
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        stopped=wait_message(client,conversation,'stopped')
        assert stopped['search_action']['status']=='cancelled'
        assert '迟到的私有地址' not in stopped['search_action']['query']


def test_search_always_exposes_real_sources_and_empty_results_do_not_claim_verification(database_url: str, tmp_path: Path) -> None:
    """模型漏引时仍提供实际搜索来源；空结果不能沿模型的错误措辞伪称已查证。"""
    empty=False
    def provider(request: httpx.Request) -> httpx.Response:
        """故意遗漏 W 引用或声称查证，验收应用基于真实结果给出的可信边界。"""
        payload=json.loads(request.content)
        if request.url.host=='search.example':
            return httpx.Response(200,json={'search_result':[] if empty else [{'title':'实际天气来源','link':'https://weather.example','content':'晴'}]})
        return reply(payload,'已经联网查证：晴。')
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        response=client.post(f'/api/conversations/{conversation}/messages',json={'content':'联网搜索：杭州天气'})
        answer=response.json()['messages'][-1]
        assert answer['web_sources'][0]['url']=='https://weather.example'
        assert '缺少可核验的网页引用' in answer['content']
        empty=True
        response=client.post(f'/api/conversations/{conversation}/messages',json={'content':'联网搜索：未知地点天气'})
        answer=response.json()['messages'][-1]
        assert '已经联网查证' not in answer['content']
        assert '未找到可核验' in answer['content'] and answer['web_sources']==[]
