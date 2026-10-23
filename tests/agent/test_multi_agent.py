"""从聊天与模型 HTTP 接口验证主从任务的真实执行。"""

import json
from pathlib import Path
from typing import Any

import httpx

from tests.auth.support import authenticated_client
from tests.attachments.test_attachments import configure_settings


def response(content: str = '', name: str = '', arguments: dict[str, Any] | None = None) -> httpx.Response:
    """构造受控模型回复，由测试传输转换成真实工具流。"""
    message: dict[str, Any] = {'content': content}
    if name:
        message['tool_calls'] = [{'id': name + '-call', 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(arguments or {})}}]
    return httpx.Response(200, json={'choices': [{'message': message}],
        'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25}})


def test_main_creates_waits_and_summarizes_without_exposing_child_text(database_url: str, tmp_path: Path) -> None:
    """子任务异步交付，主模型拿到结果后统一回答，任务状态可刷新读取。"""
    child_inputs: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """按上下文识别主从请求，子智能体只能看到任务背景。"""
        payload = json.loads(request.content)
        names = {tool['function']['name'] for tool in payload.get('tools', [])}
        if 'spawn_agent' not in names:
            child_inputs.append(payload)
            system_messages = [item for item in payload['messages'] if item.get('role') == 'system']
            assert len(system_messages) == 1
            assert '你是完成指定子任务的子智能体' in system_messages[0]['content']
            assert '优先比较成本和可维护性' in system_messages[0]['content']
            assert names == {'get_current_time', 'search_documents', 'read_document', 'search_knowledge', 'search_web', 'fetch_web'}
            return response('子任务结论：方案 A 可行。')
        tools = [json.loads(item['content']) for item in payload['messages'] if item['role'] == 'tool']
        if not tools:
            return response(name='spawn_agent', arguments={'title': '研究 A', 'task': '检查方案 A',
                'expected_output': '给出结论', 'instructions': '优先比较成本和可维护性'})
        if 'agent_id' in tools[-1]:
            return response(name='wait_agents', arguments={'agent_ids': [tools[-1]['agent_id']]})
        assert tools[-1]['agents'][0]['result'] == '子任务结论：方案 A 可行。'
        return response('主智能体建议采用方案 A。')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        sent = client.post(f'/api/conversations/{conversation}/messages', json={'content': '请研究方案 A'})
        assert sent.status_code == 200, sent.text
        answer = sent.json()['messages'][-1]
        assert answer['content'] == '主智能体建议采用方案 A。'
        assert len(child_inputs) == 1
        assert answer['agent_tasks'][0]['status'] == 'completed'
        detail = client.get(f"/api/conversations/{conversation}/messages/{answer['id']}/agents/{answer['agent_tasks'][0]['agent_id']}")
        assert detail.status_code == 200
        assert detail.json()['result'] == '子任务结论：方案 A 可行。'


def test_completed_agent_can_continue_and_wait_does_not_repeat_results(database_url: str, tmp_path: Path) -> None:
    """主智能体追加任务时保留子上下文，新执行结果只交付一次。"""
    child_calls: list[list[dict[str, Any]]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """根据已交付工具序列推进追加任务，核对第二次子调用保留第一次结论。"""
        payload = json.loads(request.content)
        if 'spawn_agent' not in {tool['function']['name'] for tool in payload.get('tools', [])}:
            child_calls.append(payload['messages'])
            system_messages = [item for item in payload['messages'] if item.get('role') == 'system']
            assert len(system_messages) == 1
            if len(child_calls) == 2:
                assert any(item.get('content') == '首次研究结论' for item in payload['messages'])
                assert '补充成本时区分一次性费用和月度费用' in system_messages[0]['content']
            return response('首次研究结论' if len(child_calls) == 1 else '已补充成本证据')
        tools = [(item['tool_call_id'], json.loads(item['content'])) for item in payload['messages'] if item['role'] == 'tool']
        if not tools:
            return response(name='spawn_agent', arguments={'title': '研究', 'task': '调查方案', 'expected_output': '给出证据'})
        name, result = tools[-1]
        identifier = tools[0][1]['agent_id']
        if name in ('spawn_agent-call', 'send_agent_message-call'):
            return response(name='wait_agents', arguments={'agent_ids': [identifier]})
        if len(child_calls) == 1:
            return response(name='send_agent_message', arguments={'agent_id': identifier, 'message': '请补充成本',
                'instructions': '补充成本时区分一次性费用和月度费用'})
        assert result['agents'][0]['result'] == '已补充成本证据'
        return response('汇总两次研究')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        identifier = client.post('/api/conversations', json={}).json()['id']
        sent = client.post(f'/api/conversations/{identifier}/messages', json={'content': '研究并检查成本'})
        assert sent.status_code == 200, sent.text
        assert sent.json()['messages'][-1]['content'] == '汇总两次研究'
        assert len(child_calls) == 2


def test_child_web_sources_are_verified_in_main_answer(database_url: str, tmp_path: Path) -> None:
    """子任务网页证据经等待交付后可以用于主回答，伪造编号仍被拒绝。"""
    from tests.search.test_search import configure_search_settings

    def provider(request: httpx.Request) -> httpx.Response:
        """子智能体真实执行搜索，主模型仅从交付结果取得来源。"""
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            return httpx.Response(200, json={'results': [{'url': 'https://example.org/evidence', 'title': '成本证据', 'summary': '每月 10 元'}]})
        tools = [json.loads(item['content']) for item in payload['messages'] if item['role'] == 'tool']
        if 'spawn_agent' not in {tool['function']['name'] for tool in payload.get('tools', [])}:
            if not tools:
                return response(name='search_web', arguments={
                    'query': '方案成本',
                    'ori_question': '请核对成本',
                })
            return response('每月10元[' + tools[-1]['results'][0]['key'] + ']')
        if not tools:
            return response(name='spawn_agent', arguments={'title': '核对成本', 'task': '搜索成本证据', 'expected_output': '成本和来源'})
        if 'agent_id' in tools[-1]:
            return response(name='wait_agents', arguments={'agent_ids': [tools[-1]['agent_id']]})
        source = tools[-1]['agents'][0]['sources'][0]
        return response('建议采用[' + source['key'] + ']，未知[W999999]')

    configure_settings(database_url, tmp_path)
    configure_search_settings(database_url, tmp_path, 'https://search.example', 'test-key')
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        identifier = client.post('/api/conversations', json={}).json()['id']
        sent = client.post(f'/api/conversations/{identifier}/messages', json={'content': '请核对成本'})
        assert sent.status_code == 200, sent.text
        answer = sent.json()['messages'][-1]
        assert [source['url'] for source in answer['web_sources']] == ['https://example.org/evidence']
        assert '[网页出处未核验]' in answer['content']
        assert 'W999999' not in answer['content']


def test_two_children_really_run_concurrently(database_url: str, tmp_path: Path) -> None:
    """两个子模型请求必须重叠执行，不能把异步创建实现为同步串行。"""
    from threading import Barrier
    barrier = Barrier(2)
    completed: set[str] = set()

    def provider(request: httpx.Request) -> httpx.Response:
        """子模型互相等待到场，主模型等待全部交付后回答。"""
        payload = json.loads(request.content)
        if 'spawn_agent' not in {tool['function']['name'] for tool in payload.get('tools', [])}:
            barrier.wait(timeout=8)
            return response('并行研究完成')
        tools = [json.loads(item['content']) for item in payload['messages'] if item['role'] == 'tool']
        if not tools:
            calls = [{'id': 'spawn-' + title, 'type': 'function', 'function': {'name': 'spawn_agent',
                'arguments': json.dumps({'title': title, 'task': title, 'expected_output': '结论'})}} for title in ('A', 'B')]
            return httpx.Response(200, json={'choices': [{'message': {'tool_calls': calls}}]})
        ids = [tool['agent_id'] for tool in tools if 'agent_id' in tool]
        for item in tools[-1].get('agents', []):
            if item.get('result'):
                completed.add(item['agent_id'])
        if len(completed) == 2:
            return response('已综合两个并行结果')
        return response(name='wait_agents', arguments={'agent_ids': ids, 'after_cursor': tools[-1].get('cursor')})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        identifier = client.post('/api/conversations', json={}).json()['id']
        sent = client.post(f'/api/conversations/{identifier}/messages', json={'content': '并行研究两项'})
        assert sent.status_code == 200, sent.text
        assert sent.json()['messages'][-1]['content'] == '已综合两个并行结果'


def test_stop_cancels_children_and_discards_late_output(database_url: str, tmp_path: Path) -> None:
    """用户停止时同时取消子任务，迟到的模型结果不能复活根回答或任务。"""
    from threading import Event
    from tests.conversation.test_streaming import wait_message
    from time import sleep
    started, release = Event(), Event()

    def provider(request: httpx.Request) -> httpx.Response:
        """让子模型阻塞，主模型通过等待工具保持运行。"""
        payload = json.loads(request.content)
        if 'spawn_agent' not in {tool['function']['name'] for tool in payload.get('tools', [])}:
            started.set()
            assert release.wait(10)
            return response('LATE_PRIVATE_CHILD_RESULT')
        tools = [json.loads(item['content']) for item in payload['messages'] if item['role'] == 'tool']
        if not tools:
            return response(name='spawn_agent', arguments={'title': '慢任务', 'task': '等待研究', 'expected_output': '结论'})
        return response(name='wait_agents', arguments={'agent_ids': [tools[0]['agent_id']]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        identifier = client.post('/api/conversations', json={}).json()['id']
        try:
            assert client.post(f'/api/conversations/{identifier}/runs', json={'content': '开始慢任务'}).status_code == 202
            assert started.wait(8)
            answer = client.get(f'/api/conversations/{identifier}').json()['messages'][-1]
            stopped = client.post(f"/api/conversations/{identifier}/messages/{answer['id']}/stop")
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()['messages'][-1]['status'] == 'stopped'
        finally:
            release.set()
        sleep(0.3)
        answer = wait_message(client, identifier, 'stopped')
        assert 'LATE_PRIVATE_CHILD_RESULT' not in answer['content']
        assert answer['agent_tasks'][0]['status'] == 'cancelled'
        event = client.get(f"/api/conversations/{identifier}/messages/{answer['id']}/events")
        assert '"status":"stopped"' in event.text


def test_child_knowledge_evidence_and_revocation_clear_task_details(database_url: str, tmp_path: Path) -> None:
    """真实知识库证据进入主引用，解绑后任务详情不能泄露旧结论。"""
    from tests.knowledge.test_knowledge_base import wait_document

    def provider(request: httpx.Request) -> httpx.Response:
        """子任务检索绑定知识库，主任务只根据已交付来源作答。"""
        payload = json.loads(request.content)
        tools = [json.loads(item['content']) for item in payload['messages'] if item['role'] == 'tool']
        if 'spawn_agent' not in {tool['function']['name'] for tool in payload.get('tools', [])}:
            if not tools:
                return response(name='search_knowledge', arguments={'query': 'maintenance'})
            source = tools[-1]['results'][0]
            return response('PRIVATE_MAINTENANCE_SUMMARY[' + source['key'] + ']')
        if not tools:
            return response(name='spawn_agent', arguments={'title': '核对维护', 'task': '查证维护要求', 'expected_output': '证据'})
        if 'agent_id' in tools[-1]:
            return response(name='wait_agents', arguments={'agent_ids': [tools[-1]['agent_id']]})
        source = tools[-1]['agents'][0]['sources'][0]
        return response('根据资料安排维护[' + source['key'] + ']，未知[K999999]')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '维护资料'}).json()['id']
        uploaded = client.post(f'/api/knowledge-bases/{base}/documents', files={'files': ('maintenance.txt', b'maintenance requires monthly checks')}).json()[0]
        wait_document(client, base, uploaded['id'])
        identifier = client.post('/api/conversations', json={}).json()['id']
        assert client.put(f'/api/conversations/{identifier}/knowledge-bases', json={'knowledge_base_ids': [base]}).status_code == 200
        sent = client.post(f'/api/conversations/{identifier}/messages', json={'content': '委托检查维护资料'})
        assert sent.status_code == 200, sent.text
        answer = sent.json()['messages'][-1]
        assert answer['knowledge_sources'][0]['filename'] == 'maintenance.txt'
        assert '[知识库出处未核验]' in answer['content']
        assert client.put(f'/api/conversations/{identifier}/knowledge-bases', json={'knowledge_base_ids': []}).status_code == 200
        task = answer['agent_tasks'][0]
        detail = client.get(f"/api/conversations/{identifier}/messages/{answer['id']}/agents/{task['agent_id']}")
        assert detail.status_code == 200
        assert detail.json()['result'] is None
        assert detail.json()['error'] == 'source_invalidated'
        assert 'PRIVATE_MAINTENANCE_SUMMARY' not in detail.text


def test_opening_revoked_history_does_not_cancel_new_answer(database_url: str, tmp_path: Path) -> None:
    """旧回答快照失效后展开详情，只清理旧回答，不影响使用新资料的执行。"""
    from threading import Event
    from tests.conversation.test_streaming import wait_message
    started, release = Event(), Event()
    children = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """第二次子研究阻塞，以便读取旧任务时检查新任务仍活动。"""
        nonlocal children
        payload = json.loads(request.content)
        tools = [json.loads(item['content']) for item in payload['messages'] if item['role'] == 'tool']
        if 'spawn_agent' not in {tool['function']['name'] for tool in payload.get('tools', [])}:
            children += 1
            if children == 2:
                started.set()
                assert release.wait(10)
            return response('研究完成')
        if not tools:
            return response(name='spawn_agent', arguments={'title': '研究', 'task': '独立研究', 'expected_output': '结论'})
        if 'agent_id' in tools[-1]:
            return response(name='wait_agents', arguments={'agent_ids': [tools[-1]['agent_id']]})
        return response('已汇总')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        identifier = client.post('/api/conversations', json={}).json()['id']
        old = client.post(f'/api/conversations/{identifier}/messages', json={'content': '首次研究'}).json()['messages'][-1]
        base = client.post('/api/knowledge-bases', json={'name': '新资料范围'}).json()['id']
        client.put(f'/api/conversations/{identifier}/knowledge-bases', json={'knowledge_base_ids': [base]})
        try:
            assert client.post(f'/api/conversations/{identifier}/runs', json={'content': '新研究'}).status_code == 202
            assert started.wait(8)
            task = old['agent_tasks'][0]
            assert client.get(f"/api/conversations/{identifier}/messages/{old['id']}/agents/{task['agent_id']}").status_code == 200
            latest = client.get(f'/api/conversations/{identifier}').json()['messages'][-1]
            assert latest['status'] == 'generating'
            assert latest['agent_tasks'][0]['status'] == 'running'
        finally:
            release.set()
        assert wait_message(client, identifier, 'complete')['content'] == '已汇总'
