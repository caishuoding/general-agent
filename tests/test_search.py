"""通过公开接口与真实 PostgreSQL 验证明确联网、外发批准和统一计量。"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_attachments import configure
from test_streaming import wait_message


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
            assert payload == {'query': '杭州天气', 'search_depth': 'basic', 'topic': 'general', 'max_results': 5, 'include_answer': False, 'include_raw_content': False, 'include_usage': True, 'auto_parameters': False}
            return httpx.Response(200, json={'results': [{'title': '官方天气', 'url': 'https://weather.example/hangzhou', 'content': '晴，18 度。'}, {'title': '不安全', 'url': 'javascript:alert(1)', 'content': '恶意'}], 'usage': {'credits': 1}})
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
        assert '设置' in absent.json()['detail'] and 'Tavily' in absent.json()['detail']
        for content in ('你好', '不要联网搜索杭州天气', '解释“联网搜索”的意思', '解释下面这句话：\n联网搜索：杭州天气', '联网是什么意思'):
            assert client.post(f'/api/conversations/{conversation}/messages', json={'content': content}).status_code == 200
        assert searches == []
        configured = client.put('/api/settings/search', json={'base_url': 'https://search.example', 'api_key': 'search-test-key'})
        assert configured.status_code == 200
        assert configured.json()['model'] == 'tavily-basic'
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
        assert search_call['estimated_cost'] is None


def test_attachment_query_requires_exact_persistent_approval_before_search(database_url: str, tmp_path: Path) -> None:
    """附件个人信息先展示精确外发文本，刷新可继续，修改使旧批准无效且重复确认不重发。"""
    searches: list[str] = []
    draft = '上海市青松路 88 号附近咖啡店'

    def provider(request: httpx.Request) -> httpx.Response:
        """规划读取真实附件；只有显式批准后才允许搜索边界接收具体查询。"""
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            searches.append(payload['query'])
            return httpx.Response(200,json={'results':[{'url':'https://cafe.example/nearby','title':'咖啡店','content':'营业时间 9–18。'}],'usage':{'credits':1}})
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
        assert 'Tavily' in action['purpose']
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


def test_real_memory_query_rejection_expiry_and_stop_never_send(database_url: str, tmp_path: Path) -> None:
    """真实 Mem0 的私有地址先确认；直接恢复、拒绝、过期及停止均不能放行查询。"""
    import psycopg
    from test_memory import configure_memory, memory_provider
    searches: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """嵌入走真实 Mem0，规划读取已保存地址，记录是否错误越过外发许可。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return memory_provider(request)
        if request.url.host=='search.example':
            searches.append(payload['query'])
            return httpx.Response(200,json={'results':[],'usage':{'credits':1}})
        assert '青松路 88 号' in str(payload['messages'])
        return reply(payload,json.dumps({'query':'青松路 88 号附近咖啡店'},ensure_ascii=False))

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        assert client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我住在青松路 88 号'}).status_code==200
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        for decision in ('reject','expire','stop'):
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
            else:
                assert client.post(f'/api/conversations/{conversation}/messages/{message["id"]}/stop').status_code==200
                assert client.post(endpoint,json={'revision':action['revision'],'decision':'approve'}).status_code==409
                assert wait_message(client,conversation,'stopped')['search_action']['status']=='cancelled'
            assert searches==[]
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


def test_search_credits_and_budget_resume_do_not_repeat_completed_search(database_url: str, tmp_path: Path) -> None:
    """每 credit 计价纳入预算，搜索完成后的回答暂停跨重启只继续最后一次模型调用。"""
    searches: list[str]=[]
    def provider(request: httpx.Request) -> httpx.Response:
        """一次真实搜索记一个 credit，最终回答以可核验的唯一来源生成。"""
        payload=json.loads(request.content)
        if request.url.host=='search.example':
            searches.append(payload['query'])
            return httpx.Response(200,json={'results':[{'url':'https://weather.example','title':'天气','content':'晴'}],'usage':{'credits':1}})
        source=json.loads(payload['messages'][0]['content'].split('\n',1)[1])[0]
        return reply(payload,f'晴。[{source["key"]}]')
    prices=[{'base_url':'https://search.example','model':'tavily-basic','per_credit':'0.05'},{'base_url':'https://model.example/v1','model':'controlled','input_per_million':'1','output_per_million':'1'}]
    transport=httpx.MockTransport(provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        client.put('/api/settings/budget',json={'monthly_limit':'0','prices':prices})
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'联网搜索：杭州天气'})
        first=wait_message(client,conversation,'paused')
        assert searches==[]
        pause=first['budget_pause']
        assert pause['reason']=='budget_reached'
        assert client.post(f'/api/budget/operations/{pause["operation_id"]}/approve',json={'pause_id':pause['pause_id']}).status_code==200
        assert client.post(f'/api/conversations/{conversation}/messages/{first["id"]}/resume').status_code==200
        second=wait_message(client,conversation,'paused')
        assert searches==['杭州天气']
        assert second['budget_pause']['pause_id']!=pause['pause_id']
        assert second['search_action']['status']=='complete'
        usage=client.get('/api/usage').json()
        assert len(usage)==1 and Decimal(usage[0]['estimated_cost'])==Decimal('.05')
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        client.put('/api/settings/budget',json={'monthly_limit':'1','prices':prices})
        assert client.post(f'/api/conversations/{conversation}/messages/{first["id"]}/resume').status_code==200
        complete=wait_message(client,conversation,'complete')
        assert complete['web_sources'][0]['url']=='https://weather.example'
        assert searches==['杭州天气']
        assert len(client.get('/api/usage').json())==2
        assert Decimal(client.get('/api/usage/summary').json()['known_cost'])==Decimal('.050015')


def test_missing_credits_remain_unknown_and_failed_search_preserves_actual_usage(database_url: str, tmp_path: Path) -> None:
    """未报告 credits 不假定固定费用，HTTP 失败也保留服务报告的用量且不伪称查证。"""
    fails=False
    def provider(request: httpx.Request) -> httpx.Response:
        """模拟 Tavily 缺失用量或带真实 credits 的拒绝响应，任何后续模型都应受预算阻止。"""
        assert request.url.host=='search.example'
        if fails:
            return httpx.Response(429,json={'error':'secret-private-provider-detail','usage':{'credits':2}})
        return httpx.Response(200,json={'results':[]})
    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test'})
        client.put('/api/settings/budget',json={'monthly_limit':'10','prices':[{'base_url':'https://search.example','model':'tavily-basic','per_credit':'.1'},{'base_url':'https://model.example/v1','model':'controlled','input_per_million':'1','output_per_million':'1'}]})
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'联网搜索：杭州天气'})
        paused=wait_message(client,conversation,'paused')
        assert paused['budget_pause']['reason']=='unknown_cost'
        assert paused['search_action']['status']=='complete'
        usage=client.get('/api/usage').json()[0]
        assert usage['credits'] is None and usage['estimated_cost'] is None
        client.post(f'/api/conversations/{conversation}/messages/{paused["id"]}/stop')
        client.put('/api/settings/budget',json={'prices':[{'base_url':'https://search.example','model':'tavily-basic','per_credit':'.1'}]})
        fails=True
        response=client.post(f'/api/conversations/{conversation}/messages',json={'content':'联网搜索：其他天气'})
        assert response.status_code==502 and '429' in response.text
        failed=wait_message(client,conversation,'failed')
        assert failed['search_action']['status']=='failed'
        assert 'secret-private-provider-detail' not in json.dumps(failed)
        call=client.get('/api/usage').json()[0]
        assert call['status']=='failed' and Decimal(call['credits'])==2
        assert Decimal(call['estimated_cost'])==Decimal('.2')


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
            searches.append(payload['query'])
            entered.set()
            assert release.wait(5)
            return httpx.Response(200,json={'results':[],'usage':{'credits':1}})
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
        while monotonic()<deadline and client.get('/api/usage/summary').json()['pending_calls']:
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
