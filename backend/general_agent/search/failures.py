"""定义联网请求的安全诊断信息和后端重试策略。"""

import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

from general_agent.infrastructure.provider import ModelFailure


WEB_MAX_ATTEMPTS = 3
WEB_MAX_RETRY_WAIT = 10.0


class WebRequestFailure(ModelFailure):
    """区分可恢复的网络故障与需要修改请求或配置的失败，不暴露原始响应正文。"""

    def __init__(
        self, message: str, *, retryable: bool, http_status: int | None = None,
        scope: Literal['request', 'provider', 'execution'] = 'request', error_code: str | None = None,
        request_id: str | None = None, retry_after: float = 0,
    ) -> None:
        """保存安全诊断和本次请求的重试元数据，尝试次数由实际外发循环更新。"""
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status
        self.scope = scope
        self.error_code = error_code
        self.request_id = request_id
        self.retry_after = retry_after
        self.attempt_count = 1

    def tool_result(self) -> dict[str, Any]:
        """无需额外输入，返回带失败范围和停止指引的工具结果；后端已完成有限重试，不让模型重开循环。"""
        guidance = ('请检查联网服务密钥或余额，本次执行已禁用搜索与抓取。' if self.scope == 'provider'
                    else '当前智能体的该类联网请求额度已耗尽，请使用已有资料或其他仍有额度的工具完成回答。' if self.scope == 'execution'
                    else '请更换 URL 或搜索条件，或根据已有资料说明限制；不要原样重复本次请求。')
        result: dict[str, Any] = {
            'error': str(self), 'retryable': False, 'upstream_retryable': self.retryable,
            'retry_managed_by': 'backend', 'failure_scope': self.scope,
            'attempt_count': self.attempt_count, 'retry_count': max(0, self.attempt_count - 1),
            'retry_limit': WEB_MAX_ATTEMPTS - 1 if self.retryable else 0,
            'request_retry_exhausted': self.retryable,
            'tool_disabled': self.scope != 'request', 'message': guidance,
        }
        for name in ('http_status', 'error_code', 'request_id'):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


def retry_after_seconds(value: str | None) -> float:
    """解析秒数或 HTTP 日期格式的 Retry-After，无效值按未指定处理。"""
    if not value:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def http_failure(response: httpx.Response) -> WebRequestFailure:
    """只对 408、429 和 5xx 允许后端重试，并提取格式受限的诊断字段。"""
    status = response.status_code
    retryable = status in (408, 429) or 500 <= status <= 599
    scope: Literal['request', 'provider'] = 'provider' if status in (401, 402) else 'request'
    descriptions = {
        400: '请求参数无效，需要修改请求', 401: 'API 密钥无效或无权访问', 402: '联网服务余额不足',
        403: '抓取被安全策略拒绝', 404: '无法获取可用网页正文，可能被站点拦截、页面不可用或无可提取内容',
        408: '请求读取超时', 413: '请求内容过大，需要缩小请求', 429: '请求过于频繁',
    }
    message = f"DeepSeek 联网请求失败（HTTP {status}）：{descriptions.get(status, '联网服务暂时不可用' if retryable else '服务拒绝该请求')}。"
    message += '后端已进行有限重试或终止等待，本次请求未取得结果。' if retryable else '该请求不可原样重试。'
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get('error') if isinstance(payload, dict) else None
    code = error.get('code') if isinstance(error, dict) else None
    detail = error.get('message') if isinstance(error, dict) else None
    error_code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else None
    match = re.search(r'request_id:\s*([A-Za-z0-9_.-]{1,100})', detail) if isinstance(detail, str) else None
    request_id = match.group(1) if match else response.headers.get('x-request-id')
    if request_id is not None and not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', request_id):
        request_id = None
    return WebRequestFailure(
        message, retryable=retryable, http_status=status, scope=scope,
        error_code=error_code, request_id=request_id,
        retry_after=retry_after_seconds(response.headers.get('retry-after')),
    )
