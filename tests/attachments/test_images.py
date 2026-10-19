"""验证图片原字节作为会话级附件直接进入模型上下文。"""

import base64
import io
import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from general_agent.app import create_app
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


def image_parts(payload: dict[str, Any]) -> list[str]:
    """读取模型实际收到的图片 data URL。"""
    parts = [part for message in payload['messages'] if isinstance(message['content'], list) for part in message['content']]
    return [part['image_url']['url'] for part in parts if part['type'] == 'image_url']


def test_png_jpeg_are_sent_directly_and_survive_restart(database_url: str, tmp_path: Path) -> None:
    """上传后的 PNG/JPEG 原字节自动进入会话上下文，重启后仍可继续追问。"""
    png, jpeg = image_bytes(), image_bytes('JPEG')

    def provider(request: httpx.Request) -> httpx.Response:
        """核对真实图片负载，不依赖 OCR 或引用白名单。"""
        payload = json.loads(request.content)
        assert set(image_parts(payload)) == {
            'data:image/png;base64,' + base64.b64encode(png).decode(),
            'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode(),
        }
        return model_response(payload, '两张图片都是红色。')

    transport = httpx.MockTransport(provider)
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, transport) as client:
        conversation = configure(client)
        uploaded = client.post('/api/conversations/{}/attachments'.format(conversation), files=[('files', ('图表.PNG', png, 'text/plain')), ('files', ('截图.jpeg', jpeg, 'image/jpeg'))])
        assert uploaded.status_code == 201
        files = client.get(f'/api/conversations/{conversation}/attachments').json()
        assert [item['status'] for item in files] == ['ready', 'ready']
        by_name = {item['filename']: item for item in files}
        assert by_name['图表.PNG']['image_metadata'] == {'format': 'PNG', 'mime': 'image/png', 'width': 24, 'height': 16}
        assert by_name['截图.jpeg']['image_metadata']['format'] == 'JPEG'
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '比较这两张图的颜色'})
        assert response.status_code == 200
        assert response.json()['messages'][-1]['content'] == '两张图片都是红色。'
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, transport) as restarted:
        assert restarted.post(f'/api/conversations/{conversation}/runs', json={'content': '再看这两张图片是什么颜色？'}).status_code == 202
        answer = wait_message(restarted, conversation, 'complete')
        assert answer['content'] == '两张图片都是红色。'


def test_corrupt_images_are_rejected_without_model_calls(database_url: str, tmp_path: Path) -> None:
    """完整性与真实格式检查拒绝假图、截断、多帧和超限。"""
    import binascii
    import struct

    png = image_bytes()
    header = struct.pack('>II', 10000, 10000) + png[24:29]
    oversized = png[:16] + header + struct.pack('>I', binascii.crc32(b'IHDR' + header)) + png[33:]
    gif = io.BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(gif, format='GIF')
    animated = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(animated, format='PNG', save_all=True, append_images=[Image.new('RGB', (8, 8), 'blue')], duration=100)
    cases = [('empty.png', b''), ('text.jpg', b'plain text'), ('gif.png', gif.getvalue()), ('jpeg.png', image_bytes('JPEG')), ('cut.png', png[:-8]), ('cut.jpg', image_bytes('JPEG')[:-2]), ('large.png', oversized), ('animated.png', animated.getvalue())]
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        for filename, raw in cases:
            response = client.post(f'/api/conversations/{conversation}/attachments', files={'files': (filename, raw)})
            assert response.status_code == 201
            attachment = next(item for item in client.get(f'/api/conversations/{conversation}/attachments').json() if item['id'] == response.json()[0]['id'])
            assert attachment['status'] == 'failed', filename
            assert attachment['image_metadata'] is None and attachment['error']
        assert client.get('/api/usage').json() == []
        assert client.get(f'/api/conversations/{conversation}').json()['messages'] == []
        many = client.post(f'/api/conversations/{conversation}/attachments', files=[('files', (f'{index}.png', png)) for index in range(6)])
        assert many.status_code == 422
        large = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('oversize.jpg', image_bytes('JPEG') + b'0' * 20_000_001)})
        assert large.status_code == 413


def test_images_remain_conversation_owned(database_url: str, tmp_path: Path) -> None:
    """上传图片只进入所属会话，其他会话必须重新上传独立副本。"""
    payloads: list[dict[str, Any]] = []
    raw = image_bytes()

    def provider(request: httpx.Request) -> httpx.Response:
        """根据实际多模态材料返回固定回答。"""
        payload = json.loads(request.content)
        payloads.append(payload)
        return model_response(payload, '红色图片。' if image_parts(payload) else '没有图片。')

    configure_settings(database_url, tmp_path)

    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        first = configure(client)
        second = client.post('/api/conversations', json={}).json()['id']
        image = client.post(f'/api/conversations/{first}/attachments', files={'files': ('private.png', raw)}).json()[0]
        assert client.post(f'/api/conversations/{first}/messages', json={'content': '看看图片'}).status_code == 200
        assert image_parts(payloads[-1])
        assert client.get(f'/api/conversations/{second}/attachments/{image["id"]}/file').status_code == 404
        assert client.post(f'/api/conversations/{second}/messages', json={'content': '看看图片'}).status_code == 200
        assert not image_parts(payloads[-1])
        copied = client.post(f'/api/conversations/{second}/attachments', files={'files': ('private.png', raw)}).json()[0]
        assert copied['id'] != image['id']
        assert client.post(f'/api/conversations/{second}/messages', json={'content': '看看图片'}).status_code == 200
        assert image_parts(payloads[-1])
        assert client.get('/api/memories').json() == []
