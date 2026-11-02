"""验证图片以本地 data URL 直接进入智谱 GLM-5.3-Flash 上下文。"""

import io
from time import monotonic, sleep

import pytest
import json
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from general_agent.skills.tools import SKILL_TOOLS
from tests.auth.support import authenticated_client
from tests.attachments.test_attachments import configure, configure_settings
from tests.conversation.test_streaming import wait_message


def image_bytes(format: str = 'PNG') -> bytes:
    """生成无个人资料的红色测试图片，使用真实编码器生成完整 PNG/JPEG。"""
    output = io.BytesIO()
    Image.new('RGB', (24, 16), 'red').save(output, format=format)
    return output.getvalue()


def model_response(payload: dict[str, Any], answer: str) -> httpx.Response:
    """用相同可计量正文模拟同步和流式模型协议。"""
    usage = {'prompt_tokens': 42, 'completion_tokens': 8, 'total_tokens': 50}
    if payload.get('stream'):
        event = {'choices': [{'delta': {'content': answer}, 'finish_reason': 'stop'}], 'usage': usage}
        return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n')
    return httpx.Response(200, json={'choices': [{'message': {'content': answer}, 'finish_reason': 'stop'}], 'usage': usage})


def image_urls(payload: dict[str, Any]) -> list[str]:
    """读取模型实际收到的智谱 image_url 内容块。"""
    parts = [part for message in payload['messages'] if isinstance(message['content'], list)
             for part in message['content']]
    return [part['image_url']['url'] for part in parts if part['type'] == 'image_url']


@pytest.mark.parametrize('streaming', [False, True])
def test_image_is_sent_as_zhipu_image_url_without_remote_upload(
    database_url: str, tmp_path: Path, streaming: bool,
) -> None:
    """上传后不发 Files 请求；模型只收到本地 data URL 和全部可用工具。"""
    payloads: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """记录模型实际收到的消息和工具，不提供文件上传接口。"""
        payload = json.loads(request.content)
        payloads.append(payload)
        return model_response(payload, '已查看图片，按当前请求回答。')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('red.png', image_bytes())})
        assert uploaded.status_code == 201
        deadline = monotonic() + 2
        while monotonic() < deadline:
            if client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status'] == 'ready':
                break
            sleep(0.02)
        assert client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status'] == 'ready'
        endpoint = 'runs' if streaming else 'messages'
        response = client.post(f'/api/conversations/{conversation}/{endpoint}', json={'content': '读取图片'})
        assert response.status_code == (202 if streaming else 200)
        if streaming:
            assert wait_message(client, conversation, 'complete')['content'] == '已查看图片，按当前请求回答。'
        assert len(payloads) == 1
        urls = image_urls(payloads[0])
        assert len(urls) == 1 and urls[0].startswith('data:image/png;base64,')
        names = {tool['function']['name'] for tool in payloads[0]['tools']}
        assert {'add_memory', 'search_memories', 'update_memory', 'delete_memory', 'search_web',
                'fetch_web', 'get_current_time', 'spawn_agent', 'wait_agents',
                'send_agent_message', 'cancel_agent'} <= names
        assert SKILL_TOOLS <= names


def test_local_images_survive_restart_without_remote_file_identity(database_url: str, tmp_path: Path) -> None:
    """重启后继续从本地附件构造图片消息，不要求重传或维护远端 ID。"""
    payloads: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """两次模型调用都读取本地图，模拟无远端 Files API。"""
        payload = json.loads(request.content)
        payloads.append(payload)
        return model_response(payload, '红色。')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        assert client.post(f'/api/conversations/{conversation}/attachments',
                           files={'files': ('red.png', image_bytes())}).status_code == 201
        for content in ('读取图片', '再次查看图片'):
            response = client.post(f'/api/conversations/{conversation}/messages', json={'content': content})
            assert response.status_code == 200, response.text

    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as restarted:
        response = restarted.post(f'/api/conversations/{conversation}/messages', json={'content': '追问颜色'})
        assert response.status_code == 200, response.text

    assert len(payloads) == 3
    assert all(len(image_urls(payload)) == 1 for payload in payloads)


def test_corrupt_images_are_rejected_without_model_calls(database_url: str, tmp_path: Path) -> None:
    """损坏图片不上传也不交给模型，返回明确的本机校验错误。"""
    calls: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """任何模型请求都应视为错误信号。"""
        calls.append(request)
        return model_response({}, '不应回答')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        assert client.post(f'/api/conversations/{conversation}/attachments',
                           files={'files': ('broken.png', b'not-an-image')}).status_code == 201
        deadline = monotonic() + 2
        while monotonic() < deadline:
            if client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status'] == 'failed':
                break
            sleep(0.02)
        assert client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status'] == 'failed'
        assert calls == []


def test_images_remain_conversation_owned(database_url: str, tmp_path: Path) -> None:
    """其他会话和账号不能读取原图，消息组装只在授权会话内进行。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """返回受控模型回答。"""
        payload = json.loads(request.content)
        assert image_urls(payload) == []
        return model_response(payload, '当前会话没有图片。')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        assert client.post(f'/api/conversations/{conversation}/attachments',
                           files={'files': ('red.png', image_bytes())}).status_code == 201
        other = client.post('/api/conversations', json={}).json()['id']
        response = client.post(f'/api/conversations/{other}/messages', json={'content': '读取图片'})
        assert response.status_code == 200
    assert response.json()['messages'][-1]['content'] == '当前会话没有图片。'
