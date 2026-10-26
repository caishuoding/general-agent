"""验证联网错误解析与外发拒绝的边界行为。"""

from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.operations import CallBudgetExceeded, OperationContext, OperationService
from general_agent.infrastructure.provider import ModelClient
from general_agent.infrastructure.settings import StoredSettings
from general_agent.search.failures import http_failure, retry_after_seconds


@pytest.mark.parametrize('status_code', [400, 401, 402, 403, 404, 413, 422])
def test_non_transient_http_failure_is_not_retryable(status_code: int) -> None:
    """非法参数、认证、余额及内容错误均不得原样重试。"""
    failure = http_failure(httpx.Response(status_code, text='private diagnostic'))
    assert failure.retryable is False
    result = failure.tool_result()
    assert result['http_status'] == status_code
    assert result['tool_disabled'] == (status_code in (401, 402))
    assert 'private diagnostic' not in str(result)


@pytest.mark.parametrize('status_code', [408, 429, 500, 502, 503, 504, 599])
def test_transient_http_failure_can_only_retry_inside_backend(status_code: int) -> None:
    """临时错误可供后端重试，但最终返回模型后不能重开同请求的重试循环。"""
    failure = http_failure(httpx.Response(status_code, json={'error': {'code': 'temporary'}}))
    assert failure.retryable is True
    failure.attempt_count = 3
    result = failure.tool_result()
    assert result['retryable'] is False and result['upstream_retryable'] is True
    assert result['request_retry_exhausted'] is True and result['retry_count'] == 2


@pytest.mark.parametrize(('value', 'expected'), [('3', 3), ('-4', 0), ('nan', 0), ('inf', 0), ('invalid', 0), (None, 0)])
def test_retry_after_rejects_invalid_or_negative_values(value: str | None, expected: float) -> None:
    """畸形退避时间不会导致无限等待，合法秒数保留。"""
    assert retry_after_seconds(value) == expected


def test_unsafe_provider_error_fields_are_not_forwarded() -> None:
    """错误正文及含换行的诊断字段不进入工具返回，避免泄露及注入。"""
    result = http_failure(httpx.Response(404, json={'error': {
        'message': 'private token', 'code': 'unsafe\ncontent',
    }}, headers={'x-request-id': 'unsafe value'})).tool_result()
    assert 'error_code' not in result and 'request_id' not in result
    assert 'private token' not in str(result)


def test_denied_outbound_cancels_only_the_unused_reservation() -> None:
    """预算拒绝后调用专用预留清理入口，不用通用状态写入覆盖可能已完成的调用。"""
    operations = MagicMock(spec=OperationService)
    call_id = uuid4()
    operations.reserve.return_value = call_id
    operations.begin.side_effect = CallBudgetExceeded('共享调用预算已耗尽。')
    gateway = CallGateway(operations, MagicMock(spec=ModelClient))
    settings = StoredSettings(base_url='https://search.example', model='deepseek-search', api_key='test-key')
    with pytest.raises(CallBudgetExceeded), gateway.bind(OperationContext(uuid4(), 'test')).call(settings, 'credits'):
        pytest.fail('被拒绝的调用不应进入外发代码。')
    operations.cancel_reservation.assert_called_once_with(call_id)
    operations.record.assert_not_called()
