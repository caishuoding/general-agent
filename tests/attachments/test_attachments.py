"""验证附件上传后以完整文字进入当前会话上下文。"""

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app


def configure(client: TestClient) -> str:
    """配置固定模型并创建用于资料问答的独立会话。"""
    client.put('/api/settings', json={'base_url': 'https://model.example/v1', 'model': 'controlled', 'api_key': 'test-key'})
    return str(client.post('/api/conversations', json={}).json()['id'])


def attachment_documents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从实际模型请求中读取附件全文，避免依赖旧的引用白名单。"""
    for message in payload['messages']:
        content = message.get('content')
        if not isinstance(content, str) or '"attachment_id"' not in content:
            continue
        try:
            data = json.loads(content)
        except ValueError:
            continue
        if isinstance(data, list):
            return data
    return []


def stream_answer(content: str) -> httpx.Response:
    """构造流式模型响应，供重启后的追问复用。"""
    event = {'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}}
    return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n', headers={'Content-Type': 'text/event-stream'})


def test_uploaded_text_is_sent_in_full_and_survives_restart(database_url: str, tmp_path: Path) -> None:
    """未绑定消息的附件也会进入当前会话，升级后的全文在重启后仍可用于追问。"""
    requests: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """核对模型实际收到完整原文，而不是预先筛选的片段。"""
        payload = json.loads(request.content)
        requests.append(payload)
        documents = attachment_documents(payload)
        assert len(documents) == 1
        assert '蓝鲸项目于星期五交付。\n负责人是林女士。' in documents[0]['content']
        if payload.get('stream'):
            return stream_answer('蓝鲸项目星期五交付。')
        return httpx.Response(200, json={'choices': [{'message': {'content': '蓝鲸项目星期五交付。'}}]})

    transport = httpx.MockTransport(provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
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

    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as restarted:
        response = restarted.post(f'/api/conversations/{conversation}/runs', json={'content': '请再告诉我交付时间'})
        assert response.status_code == 202
        from tests.conversation.test_streaming import wait_message
        message = wait_message(restarted, conversation, 'complete')
        assert message['content'] == '蓝鲸项目星期五交付。'
        assert len(requests) == 2


def test_attachments_are_isolated_by_conversation(database_url: str, tmp_path: Path) -> None:
    """未选择消息也能使用本会话附件，但其他会话不能读取或接收这份原文。"""
    payloads: list[dict[str, Any]] = []
    secret = 'PRIVATE-CONVERSATION-SECRET'

    def provider(request: httpx.Request) -> httpx.Response:
        """记录每个会话实际收到的附件全文，返回普通回答。"""
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(200, json={'choices': [{'message': {'content': '已收到资料。'}}]})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
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

    with TestClient(
        create_app(database_url, tmp_path, httpx.MockTransport(provider)),
        base_url='http://127.0.0.1:8000',
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

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        assert client.post(f'/api/conversations/{conversation}/attachments', files=[('files', (f'{index}.txt', b'text')) for index in range(6)]).status_code == 422
        assert client.post(f'/api/conversations/{conversation}/attachments', files=[('files', (f'{index}.txt', b'text')) for index in range(5)]).status_code == 201
        failed = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('empty.txt', b' \n\t')}).json()[0]
        row = next(item for item in client.get(f'/api/conversations/{conversation}/attachments').json() if item['id'] == failed['id'])
        assert row['status'] == 'failed' and row['error']
        assert client.post(f'/api/conversations/{conversation}/messages', json={'content': '读一下资料'}).status_code == 200
        assert calls == 1


def test_long_document_tail_is_included_in_full_context(database_url: str, tmp_path: Path) -> None:
    """长文本末尾的事实也进入上下文，不依赖关键词片段筛选。"""
    sentinel = '玄武口令为海风。'
    text = '普通背景。' * 5000 + sentinel

    def provider(request: httpx.Request) -> httpx.Response:
        """从实际请求中核对文末事实存在。"""
        payload = json.loads(request.content)
        documents = attachment_documents(payload)
        assert documents and sentinel in documents[0]['content']
        return httpx.Response(200, json={'choices': [{'message': {'content': '口令是海风。'}}]})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('long.txt', text.encode())})
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '文末说了什么？'})
        assert response.status_code == 200
        assert response.json()['messages'][-1]['content'] == '口令是海风。'
