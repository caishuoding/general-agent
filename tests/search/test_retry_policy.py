"""通过聊天和受控外部 HTTP 验证联网错误分类、重复拦截与后端重试。"""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.auth.support import authenticated_client
from tests.attachments.test_attachments import configure
from tests.search.test_search import configure_search_settings, streaming_transport, structured_text, tool_call, tool_messages


@pytest.mark.parametrize('status_code', [400, 403, 404, 413])
def test_permanent_failure_blocks_repeated_url_but_allows_another(
    database_url: str, tmp_path: Path, status_code: int,
) -> None:
    """同一失败 URL 跨调用 ID 只外发一次，其他 URL 仍可读取且模型收到诊断字段。"""
    urls: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """第一次拒绝目标页面，第二次允许其他页面，并故意让模型重复失败参数。"""
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            urls.append(payload['url'])
            if payload['url'] == 'https://blocked.example/page':
                return httpx.Response(status_code, json={'error': {
                    'message': 'private-provider-detail (request_id: request-404)',
                    'type': 'not_found_error', 'code': 'invalid_request_error',
                }})
            return httpx.Response(200, json={'title': '可读页面', 'content': '已核验正文'})
        messages = tool_messages(payload)
        if not messages:
            return tool_call('fetch_web', {'url': 'https://blocked.example/page'}, 'first')
        error = json.loads(messages[-1]['content'])
        if len(messages) <= 2:
            assert error['http_status'] == status_code
            assert error['retryable'] is False
            assert error['request_id'] == 'request-404'
            assert error['error_code'] == 'invalid_request_error'
            assert 'private-provider-detail' not in messages[-1]['content']
            if len(messages) == 1:
                return tool_call('fetch_web', {'url': 'https://blocked.example/page'}, 'repeat')
            assert error['cached_failure'] is True
            return tool_call('fetch_web', {'url': 'https://other.example/page'}, 'other')
        key = error['results'][0]['key']
        return structured_text(f'其他页面可读取。[{key}]')

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取网页'}).json()['messages'][-1]
        assert answer['status'] == 'complete'
        assert urls == ['https://blocked.example/page', 'https://other.example/page']
        assert len(answer['web_sources']) == 1


@pytest.mark.parametrize('status_code', [401, 402])
def test_credentials_or_balance_failure_disables_both_web_tools(
    database_url: str, tmp_path: Path, status_code: int,
) -> None:
    """密钥或余额错误后两个联网工具都隐藏，模型强行换工具也不会再次外发。"""
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """返回供应商级失败后，故意让模型调用另一联网工具以检查后端拦截。"""
        nonlocal attempts
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            attempts += 1
            return httpx.Response(status_code, json={'error': {'code': 'authentication_error'}})
        messages = tool_messages(payload)
        if not messages:
            return tool_call('fetch_web', {'url': 'https://example.com'}, 'fetch')
        names = {tool['function']['name'] for tool in payload['tools']}
        assert not names.intersection({'search_web', 'fetch_web'})
        result = json.loads(messages[-1]['content'])
        assert result['tool_disabled'] is True and result['retryable'] is False
        if len(messages) == 1:
            return tool_call('search_web', {'query': '换个工具', 'ori_question': '查询网页'}, 'forged')
        assert result['cached_failure'] is True
        return structured_text('联网配置需要处理。')

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '查询网页'}).json()['messages'][-1]
        assert answer['status'] == 'complete'
        assert attempts == 1


@pytest.mark.parametrize('status_code', [408, 429, 500, 503])
def test_transient_failure_is_retried_before_model_receives_result(
    database_url: str, tmp_path: Path, status_code: int,
) -> None:
    """临时失败由后端重试，每次真实请求单独计量，模型只收到最终成功结果。"""
    attempts = 0
    model_turns = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """第一次临时失败、第二次成功，禁止模型额外发起重试。"""
        nonlocal attempts, model_turns
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            attempts += 1
            if attempts == 1:
                return httpx.Response(status_code, headers={'Retry-After': '0'}, json={'error': {'code': 'temporary'}})
            return httpx.Response(200, json={'title': '新闻', 'content': '可读取正文'})
        model_turns += 1
        messages = tool_messages(payload)
        if not messages:
            return tool_call('fetch_web', {'url': 'https://news.example'}, 'fetch')
        result = json.loads(messages[-1]['content'])
        assert 'error' not in result
        return structured_text(f"新闻已核验。[{result['results'][0]['key']}]")

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取新闻'}).json()['messages'][-1]
        assert answer['status'] == 'complete'
        assert attempts == 2 and model_turns == 2
        usage = [item for item in client.get('/api/usage').json() if item['unit'] == 'credits']
        assert sorted(Decimal(item['credits']) for item in usage) == [0, 1]


def test_exhausted_backend_retries_are_not_restarted_by_model(database_url: str, tmp_path: Path) -> None:
    """后端最多初次加两次重试，模型再次提交同一参数时不会开启下一组重试。"""
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """持续返回 503，并在下一模型轮次强行重复该 URL。"""
        nonlocal attempts
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            attempts += 1
            return httpx.Response(503, json={'error': {'code': 'temporary'}})
        messages = tool_messages(payload)
        if not messages:
            return tool_call('fetch_web', {'url': 'https://down.example'}, 'first')
        result = json.loads(messages[-1]['content'])
        assert result['request_retry_exhausted'] is True
        assert result['attempt_count'] == 3
        if len(messages) == 1:
            return tool_call('fetch_web', {'url': 'https://down.example'}, 'repeat')
        assert result['cached_failure'] is True
        return structured_text('该网页暂时不可读取。')

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取网页'}).json()['messages'][-1]
        assert answer['status'] == 'complete'
        assert attempts == 3


def test_stop_during_backoff_prevents_another_outbound_request(database_url: str, tmp_path: Path) -> None:
    """退避等待期间停止回答后，不会再外发重试，也不会发出新的模型请求。"""
    from threading import Event
    from time import monotonic, sleep

    failed = Event()
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """用较长 Retry-After 为停止请求提供稳定窗口。"""
        nonlocal attempts
        if request.url.host == 'search.example':
            attempts += 1
            failed.set()
            return httpx.Response(429, headers={'Retry-After': '4'}, json={'error': {'code': 'rate_limit'}})
        return tool_call('fetch_web', {'url': 'https://news.example'}, 'fetch')

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        started = client.post(f'/api/conversations/{conversation}/runs', json={'content': '读取网页'})
        assert started.status_code == 202
        answer_id = started.json()['messages'][-1]['id']
        assert failed.wait(5)
        stopped = client.post(f'/api/conversations/{conversation}/messages/{answer_id}/stop')
        assert stopped.status_code == 200
        deadline = monotonic() + 3
        while monotonic() < deadline:
            usage = client.get('/api/usage').json()
            if not any(item['status'] == 'generating' for item in usage):
                break
            sleep(0.02)
        assert attempts == 1
        answer = client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
        assert answer['status'] == 'stopped'


def test_large_retry_after_does_not_retry_earlier_than_requested(database_url: str, tmp_path: Path) -> None:
    """供应商要求长时间等待时直接返回当前失败，既不提前重试也不长时间阻塞线程。"""
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """模拟超出本次可等待时长的 Retry-After。"""
        nonlocal attempts
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            attempts += 1
            return httpx.Response(429, headers={'Retry-After': '300'}, json={'error': {'code': 'rate_limit'}})
        messages = tool_messages(payload)
        if not messages:
            return tool_call('fetch_web', {'url': 'https://news.example'}, 'fetch')
        result = json.loads(messages[-1]['content'])
        assert result['attempt_count'] == 1 and result['retryable'] is False
        return structured_text('服务繁忙，请稍后重新查询。')

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取网页'}).json()['messages'][-1]
        assert answer['status'] == 'complete'
        assert attempts == 1


def test_new_answer_does_not_reuse_previous_non_retryable_failure(database_url: str, tmp_path: Path) -> None:
    """失败拦截只属于当前执行，新用户消息可以重新读取同一网页。"""
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """第一轮返回 404，下一条用户消息读取相同网页时返回正文。"""
        nonlocal attempts
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            attempts += 1
            if attempts == 1:
                return httpx.Response(404, json={'error': {'code': 'not_found'}})
            return httpx.Response(200, json={'title': '恢复后的页面', 'content': '现在可读取'})
        last_user = max(index for index, item in enumerate(payload['messages']) if item['role'] == 'user')
        current = [item for item in payload['messages'][last_user + 1:] if item['role'] == 'tool']
        if not current:
            return tool_call('fetch_web', {'url': 'https://news.example'}, f'fetch-{attempts}')
        result = json.loads(current[-1]['content'])
        return structured_text('暂时无法读取。' if 'error' in result else f"现在可读取。[{result['results'][0]['key']}]")

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        for content in ('读取网页', '再读取一次'):
            answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': content}).json()['messages'][-1]
            assert answer['status'] == 'complete'
        assert attempts == 2
        assert '现在可读取' in answer['content']


def test_backend_retry_checks_agent_budget_and_finishes_reservation(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抓取重试耗尽共享额度后同时禁用搜索与抓取，且不遗留未终结预留。"""
    from general_agent.infrastructure.agent_limits import LIMITS

    monkeypatch.setattr('general_agent.infrastructure.operations.LIMITS', replace(LIMITS, web_calls=2))
    attempts = 0
    search_attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """输入模型或联网请求，输出耗尽共享额度的抓取和被后端拦截的搜索。"""
        nonlocal attempts, search_attempts
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            if 'url' in payload:
                attempts += 1
                return httpx.Response(503, json={'error': {'code': 'temporary'}})
            search_attempts += 1
            return httpx.Response(200, json={'results': [{'url': 'https://news.example/available',
                                                          'title': '可用来源', 'summary': '已核验'}]})
        messages = tool_messages(payload)
        if not messages:
            return tool_call('fetch_web', {'url': 'https://news.example'}, 'fetch')
        if len(messages) == 1:
            assert '智能体已达到2次联网工具请求上限' in messages[-1]['content']
            result = json.loads(messages[-1]['content'])
            assert result['tool_disabled'] is True and result['error_code'] == 'call_budget_exhausted'
            names = {tool['function']['name'] for tool in payload['tools']}
            assert not names.intersection({'search_web', 'fetch_web'})
            return tool_call('search_web', {'query': '可用来源', 'ori_question': '查询可用来源'}, 'search')
        blocked = json.loads(messages[-1]['content'])
        assert '已达到最大重试次数' in blocked['error']
        return structured_text('共享联网额度已耗尽，本轮不继续搜索。')

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取网页'}).json()['messages'][-1]
        assert answer['status'] == 'complete' and attempts == 2 and search_attempts == 0
        usage = [item for item in client.get('/api/usage').json() if item['unit'] == 'credits']
        # 用量公开接口只显示真实已发请求，未外发的 cancelled 记录不会伪装成计费请求。
        assert len(usage) == 2
        assert sorted(Decimal(item['credits']) for item in usage) == [0, 0]


def test_parallel_subagents_receive_errors_and_independently_block_repeated_fetches(
    database_url: str, tmp_path: Path,
) -> None:
    """两个子执行均收到真实 404，各自只请求一次失败 URL 并可交付其他页面的证据。"""
    from threading import Lock

    from tests.agent.test_multi_agent import agent_deliveries

    urls: list[str] = []
    lock = Lock()

    def provider(request: httpx.Request) -> httpx.Response:
        """主模型创建两个子任务，子模型故意重复失败请求后读取可用替代页面。"""
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            with lock:
                urls.append(payload['url'])
            if payload['url'] == 'https://blocked.example':
                return httpx.Response(404, json={'error': {'code': 'not_found'}})
            return httpx.Response(200, json={'title': '可用证据', 'content': '经核验的事实'})
        main = any(tool['function']['name'] == 'spawn_agent' for tool in payload.get('tools', []))
        messages = tool_messages(payload)
        if main:
            deliveries = agent_deliveries(payload)
            if len(deliveries) == 2:
                keys = [item['sources'][0]['key'] for item in deliveries]
                return structured_text('主回答已汇总。' + ''.join(f'[{key}]' for key in keys))
            if messages:
                return tool_call('wait_agents', {}, f'wait-{len(messages)}')
            calls: list[dict[str, Any]] = []
            for name in ('A', 'B'):
                result = tool_call('spawn_agent', {
                    'title': name, 'task': f'核查 {name}', 'expected_output': '结论与引用',
                }, f'spawn-{name}').json()
                calls.extend(result['choices'][0]['message']['tool_calls'])
            return httpx.Response(200, json={'choices': [{'message': {'content': None, 'tool_calls': calls}}]})
        if not messages:
            return tool_call('fetch_web', {'url': 'https://blocked.example'}, 'first')
        result = json.loads(messages[-1]['content'])
        if len(messages) == 1:
            assert result['http_status'] == 404 and result['retryable'] is False
            return tool_call('fetch_web', {'url': 'https://blocked.example'}, 'repeat')
        if len(messages) == 2:
            assert result['cached_failure'] is True
            return tool_call('fetch_web', {'url': 'https://good.example'}, 'alternate')
        return structured_text(f"子任务已核验。[{result['results'][0]['key']}]")

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        sent = client.post(f'/api/conversations/{conversation}/messages', json={'content': '并行核查两份资料'})
        assert sent.status_code == 200, sent.text
        answer = sent.json()['messages'][-1]
        assert answer['status'] == 'complete'
        assert len(answer['agent_tasks']) == 2
        assert all(task['status'] == 'completed' for task in answer['agent_tasks'])
        assert urls.count('https://blocked.example') == 2
        assert urls.count('https://good.example') == 2
        assert len(answer['web_sources']) == 2


def test_five_different_404s_do_not_disable_fetch_for_a_valid_url(database_url: str, tmp_path: Path) -> None:
    """五次失效页面后第六次仍可抓取；第七次搜索会被共享额度拦截。"""
    outbound: list[str] = []
    search_attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """依次返回五次 404、一次有效正文，并尝试超额的搜索调用。"""
        nonlocal search_attempts
        payload = json.loads(request.content)
        if request.url.host == 'search.example':
            if 'url' not in payload:
                search_attempts += 1
                return httpx.Response(200, json={'results': []})
            outbound.append(payload['url'])
            if '/missing/' in payload['url']:
                return httpx.Response(404, json={'error': {'code': 'not_found'}})
            return httpx.Response(200, json={'title': '可用页面', 'content': '真实正文'})
        messages = tool_messages(payload)
        if len(messages) < 6:
            names = {tool['function']['name'] for tool in payload['tools']}
            assert 'fetch_web' in names
            if messages:
                error = json.loads(messages[-1]['content'])
                assert error['http_status'] == 404 and error['retry_limit'] == 0
            url = f'https://news.example/missing/{len(messages)}' if len(messages) < 5 else 'https://news.example/good'
            return tool_call('fetch_web', {'url': url}, f'fetch-{len(messages)}')
        result = json.loads(messages[-1]['content'])
        if len(messages) == 6:
            assert {'search_web', 'fetch_web'}.issubset({tool['function']['name'] for tool in payload['tools']})
            return tool_call('search_web', {'query': '超额搜索', 'ori_question': '验证共享额度'}, 'search-over-limit')
        assert result['error_code'] == 'call_budget_exhausted'
        assert not {tool['function']['name'] for tool in payload['tools']}.intersection({'search_web', 'fetch_web'})
        source = json.loads(messages[-2]['content'])['results'][0]['key']
        return structured_text(f'已读到可用页面。[{source}]')

    configure_search_settings(database_url, tmp_path, 'https://search.example')
    with authenticated_client(database_url, tmp_path, streaming_transport(provider)) as client:
        conversation = configure(client)
        answer = client.post(f'/api/conversations/{conversation}/messages', json={'content': '核查这些网页'}).json()['messages'][-1]
        assert answer['status'] == 'complete'
        assert len(outbound) == 6 and outbound[-1] == 'https://news.example/good' and search_attempts == 0
        assert answer['content'].startswith('已读到可用页面。') and len(answer['web_sources']) == 1
