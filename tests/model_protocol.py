"""为受控模型替身提供统一的工具回合辅助。"""

import json
from collections.abc import Callable
from typing import Any

import httpx


def streaming_transport(provider: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    """把非流式受控回复转换成 SSE，供统一流式循环测试使用。"""
    def wrapped(request: httpx.Request) -> httpx.Response:
        """保留真实搜索响应，并只在聊天流请求中转换受控消息。"""
        response = provider(request)
        try:
            payload = json.loads(request.content)
        except (TypeError, ValueError):
            return response
        if not payload.get('stream') or request.url.host == 'search.example':
            return response
        if response.headers.get('content-type', '').startswith('text/event-stream'):
            return response
        if response.content and response.text.startswith('data:'):
            return response
        data = response.json()
        message = data['choices'][0]['message']
        calls = message.get('tool_calls') or []
        events: list[dict[str, Any]] = []
        if calls:
            for index, call in enumerate(calls):
                function = call['function']
                events.append({'choices': [{'delta': {'tool_calls': [{'index': index, 'id': call['id'], 'type': 'function', 'function': {'name': function['name'], 'arguments': ''}}]}}]})
                events.append({'choices': [{'delta': {'tool_calls': [{'index': index, 'function': {'arguments': function['arguments']}}]}}]})
            events.append({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}], 'usage': data.get('usage')})
        else:
            events.append({'choices': [{'delta': {'content': message.get('content')}, 'finish_reason': 'stop'}], 'usage': data.get('usage')})
        body = ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events) + 'data: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={'Content-Type': 'text/event-stream'})
    return httpx.MockTransport(wrapped)


class StreamingMockTransport(httpx.MockTransport):
    """测试专用 MockTransport，把 JSON 聊天回复统一转换成 SSE。"""

    def __init__(self, provider: Callable[[httpx.Request], httpx.Response]) -> None:
        """包装原受控提供器，不改变非流式、搜索和错误响应。"""
        super().__init__(provider)
        self.provider = provider

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """执行受控提供器，并在聊天流请求需要时转换 JSON 响应。"""
        response = self.provider(request)
        try:
            payload = json.loads(request.content)
        except (TypeError, ValueError):
            return response
        if (
            not payload.get('stream')
            or request.url.host == 'search.example'
            or not response.is_success
        ):
            return response
        if isinstance(response.stream, httpx.ByteStream) and response.text.startswith('data:'):
            if not response.headers.get('content-type'):
                response.headers['Content-Type'] = 'text/event-stream'
            return response
        if not response.headers.get('content-type', '').startswith('application/json'):
            return response
        data = response.json()
        try:
            message = data['choices'][0]['message']
        except (KeyError, IndexError, TypeError):
            return response
        calls = message.get('tool_calls') or []
        events: list[dict[str, Any]] = []
        if calls:
            for index, call in enumerate(calls):
                function = call['function']
                events.append({'choices': [{'delta': {'tool_calls': [{'index': index, 'id': call['id'], 'type': 'function', 'function': {'name': function['name'], 'arguments': ''}}]}}]})
                events.append({'choices': [{'delta': {'tool_calls': [{'index': index, 'function': {'arguments': function['arguments']}}]}}]})
            events.append({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}], 'usage': data.get('usage')})
        else:
            events.append({'choices': [{'delta': {'content': message.get('content')}, 'finish_reason': 'stop'}], 'usage': data.get('usage')})
        body = ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events) + 'data: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={'Content-Type': 'text/event-stream'})

