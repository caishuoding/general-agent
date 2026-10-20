"""验证附件仅自动提供清单，模型通过工具获取当前会话原文。"""

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.auth.support import authenticated_client, set_global_settings


def configure(client: TestClient) -> str:
    """使用已保存的全局模型创建用于资料问答的独立会话。"""
    return str(client.post('/api/conversations', json={}).json()['id'])


def configure_settings(database_url: str, tmp_path: Path) -> None:
    """通过管理员入口配置资料测试使用的全局模型。"""
    set_global_settings(
        database_url,
        tmp_path,
        {"base_url": "https://model.example/v1", "model": "controlled", "api_key": "test-key"},
    )


def attachment_documents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从实际模型请求读取附件清单，并确保没有自动注入正文和解析说明。"""
    for message in payload['messages']:
        content = message.get('content')
        if not isinstance(content, str) or '"document_id"' not in content:
            continue
        try:
            data = json.loads(content)
        except ValueError:
            continue
        if isinstance(data, list):
            assert all('content' not in item and 'parse_info' not in item for item in data)
            return data
    return []


def document_tool_reply(payload: dict[str, Any], name: str = 'read_document', **arguments: Any) -> httpx.Response | None:
    """在最新用户消息之后模拟自主调用文档工具，工具已返回时交由测试核验正文。"""
    last_user = max(index for index, message in enumerate(payload['messages']) if message['role'] == 'user')
    if any(message['role'] == 'tool' for message in payload['messages'][last_user + 1:]):
        return None
    documents = attachment_documents(payload)
    if not documents:
        return None
    if name == 'read_document':
        arguments.setdefault('document_id', documents[-1]['document_id'])
    return httpx.Response(200, json={'choices': [{'message': {'content': None, 'tool_calls': [
        {'id': 'document-call', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}
    ]}}]})


def document_tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    """读取最近一次真实工具返回，测试只能依据实际返回正文断言资料可用。"""
    result = json.loads(next(message['content'] for message in reversed(payload['messages']) if message['role'] == 'tool'))
    assert isinstance(result, dict)
    return result


def stream_answer(content: str) -> httpx.Response:
    """构造流式模型响应，供重启后的追问复用。"""
    event = {'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}}
    return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n', headers={'Content-Type': 'text/event-stream'})


def test_uploaded_text_is_read_on_demand_and_survives_restart(database_url: str, tmp_path: Path) -> None:
    """首轮和重启后均只自动提供清单，模型调用后取得持久化原文。"""
    requests: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """先确认清单不含正文，再请求原文并根据实际结果回答。"""
        payload = json.loads(request.content)
        requests.append(payload)
        documents = attachment_documents(payload)
        assert len(documents) == 1
        tool_reply = document_tool_reply(payload)
        if tool_reply is not None:
            assert '蓝鲸项目于星期五交付。' not in str(payload['messages'])
            return tool_reply
        result = document_tool_result(payload)
        assert ''.join(block['content'] for block in result['content']) == '蓝鲸项目于星期五交付。\n负责人是林女士。'
        if payload.get('stream'):
            return stream_answer('蓝鲸项目星期五交付。')
        return httpx.Response(200, json={'choices': [{'message': {'content': '蓝鲸项目星期五交付。'}}]})

    transport = httpx.MockTransport(provider)
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, transport) as client:
        conversation = configure(client)
        uploaded = client.post(
            f'/api/conversations/{conversation}/attachments',
            files={'files': ('计划.md', '蓝鲸项目于星期五交付。\n负责人是林女士。'.encode(), 'text/markdown')},
        )
        assert uploaded.status_code == 201
        attachment = client.get(f'/api/conversations/{conversation}/attachments').json()[0]
        assert attachment['status'] == 'ready'
        assert 'content' not in attachment
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '蓝鲸项目什么时候交付？'})
        assert response.status_code == 200, response.text
        assert response.json()['messages'][-1]['content'] == '蓝鲸项目星期五交付。'

    with authenticated_client(database_url, tmp_path, transport) as restarted:
        response = restarted.post(f'/api/conversations/{conversation}/runs', json={'content': '请再告诉我交付时间'})
        assert response.status_code == 202
        from tests.conversation.test_streaming import wait_message
        message = wait_message(restarted, conversation, 'complete')
        assert message['content'] == '蓝鲸项目星期五交付。'
        assert len(requests) == 4


def test_attachments_are_isolated_by_conversation(database_url: str, tmp_path: Path) -> None:
    """文档只通过工具进入所属会话，其他会话没有清单或原文。"""
    payloads: list[dict[str, Any]] = []
    secret = 'PRIVATE-CONVERSATION-SECRET'

    def provider(request: httpx.Request) -> httpx.Response:
        """自主请求有清单会话的原文，记录真实模型消息以核对隔离。"""
        payload = json.loads(request.content)
        payloads.append(payload)
        tool_reply = document_tool_reply(payload)
        if tool_reply is not None:
            return tool_reply
        return httpx.Response(200, json={'choices': [{'message': {'content': '已收到资料。'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        first = configure(client)
        second = client.post('/api/conversations', json={}).json()['id']
        uploaded = client.post(f'/api/conversations/{first}/attachments', files={'files': ('private.txt', secret.encode())}).json()[0]
        assert client.post(f'/api/conversations/{first}/messages', json={'content': '资料内容是什么？'}).status_code == 200
        assert secret in str(payloads[-1])
        assert client.get(f'/api/conversations/{second}/attachments/{uploaded["id"]}/file').status_code == 404
        assert client.post(f'/api/conversations/{second}/messages', json={'content': '资料内容是什么？'}).status_code == 200
        assert secret not in str(payloads[-1])
        copied = client.post(f'/api/conversations/{second}/attachments', files={'files': ('private.txt', secret.encode())}).json()[0]
        assert copied['id'] != uploaded['id']
        assert client.post(f'/api/conversations/{second}/messages', json={'content': '资料内容是什么？'}).status_code == 200
        assert secret in str(payloads[-1])


def test_message_history_shows_attachments_used_by_each_user_message(
    database_url: str,
    tmp_path: Path,
) -> None:
    """上传后发送的用户消息在会话详情中保留附件引用，后续消息不重复展示旧附件。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """规划阶段直接要求回答，正式回合返回固定文本以完成消息持久化。"""
        json.loads(request.content)
        return httpx.Response(200, json={'choices': [{'message': {'content': '已阅读资料。'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(
        database_url, tmp_path, httpx.MockTransport(provider)
    ) as client:
        conversation = configure(client)
        uploaded = client.post(
            f'/api/conversations/{conversation}/attachments',
            files={'files': ('计划.md', '蓝鲸项目于星期五交付。'.encode(), 'text/markdown')},
        )
        assert uploaded.status_code == 201
        first = client.post(
            f'/api/conversations/{conversation}/messages',
            json={'content': '请阅读计划'},
        )
        assert first.status_code == 200
        second = client.post(
            f'/api/conversations/{conversation}/messages',
            json={'content': '再概括一次'},
        )
        assert second.status_code == 200
        detail = client.get(f'/api/conversations/{conversation}').json()
        user_messages = [message for message in detail['messages'] if message['role'] == 'user']
        assert [item['filename'] for item in user_messages[0]['attachments']] == ['计划.md']
        assert user_messages[1]['attachments'] == []


def test_upload_limits_and_failed_files_are_truthful(database_url: str, tmp_path: Path) -> None:
    """上传数量、空文字和失败状态仍由后端执行，失败文件不会进入模型资料。"""
    calls = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """确认失败文件没有伪装成可用资料。"""
        nonlocal calls
        payload = json.loads(request.content)
        calls += 1
        assert 'empty' not in str(payload['messages'])
        return httpx.Response(200, json={'choices': [{'message': {'content': '没有可读取资料。'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        assert client.post(f'/api/conversations/{conversation}/attachments', files=[('files', (f'{index}.txt', b'text')) for index in range(6)]).status_code == 422
        assert client.post(f'/api/conversations/{conversation}/attachments', files=[('files', (f'{index}.txt', b'text')) for index in range(5)]).status_code == 201
        failed = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('empty.txt', b' \n\t')}).json()[0]
        row = next(item for item in client.get(f'/api/conversations/{conversation}/attachments').json() if item['id'] == failed['id'])
        assert row['status'] == 'failed' and row['error']
        assert client.post(f'/api/conversations/{conversation}/messages', json={'content': '读一下资料'}).status_code == 200
        assert calls == 1


def test_long_document_tail_is_retrieved_on_demand(database_url: str, tmp_path: Path) -> None:
    """长文本末尾事实通过检索切片返回，首轮不能自动携带全文。"""
    sentinel = '玄武口令为海风。'
    text = '普通背景。' * 5000 + sentinel

    def provider(request: httpx.Request) -> httpx.Response:
        """首轮自主检索文末事实，后续只根据实际返回切片回答。"""
        payload = json.loads(request.content)
        tool_reply = document_tool_reply(payload, 'search_documents', query='玄武口令')
        if tool_reply is not None:
            assert sentinel not in str(payload['messages'])
            return tool_reply
        result = document_tool_result(payload)
        assert any(sentinel in item['content'] for item in result['results'])
        return httpx.Response(200, json={'choices': [{'message': {'content': '口令是海风。'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('long.txt', text.encode())})
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '文末说了什么？'})
        assert response.status_code == 200
        assert response.json()['messages'][-1]['content'] == '口令是海风。'
