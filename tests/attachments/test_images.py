"""通过公开上传和聊天接口验证真实图片字节、视觉能力与会话出处。"""

import base64
import io
import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from general_agent.app import create_app
from tests.attachments.test_attachments import configure
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


def test_png_jpeg_visual_answers_have_real_image_sources_and_survive_restart(database_url: str, tmp_path: Path) -> None:
    """PNG/JPEG 原字节经已验证模型回答，重启后的图片追问仍引用同一图片本身。"""
    png, jpeg = image_bytes(), image_bytes('JPEG')
    payloads: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """基础能力检查识别红色；正式请求核对真实图片负载及稳定来源。"""
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payload['messages']) == 1:
            return model_response(payload, 'red')
        sources = json.loads(payload['messages'][0]['content'].split('\n', 1)[1])
        parts = [part for message in payload['messages'] if isinstance(message['content'], list) for part in message['content']]
        data = [part['image_url']['url'] for part in parts if part['type'] == 'image_url']
        assert set(data) == {'data:image/png;base64,' + base64.b64encode(png).decode(), 'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode()}
        assert {source['location'] for source in sources} == {'图片本身'}
        return model_response(payload, '两张图片都是红色。' + ''.join(f'[{source["key"]}]' for source in sources) + '[A99999]')

    transport = httpx.MockTransport(provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        assert client.post('/api/settings/capabilities/image').json()['status'] == 'passed'
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files=[('files', ('图表.PNG', png, 'text/plain')), ('files', ('截图.jpeg', jpeg, 'image/jpeg'))])
        assert uploaded.status_code == 201
        files = client.get(f'/api/conversations/{conversation}/attachments').json()
        assert [item['status'] for item in files] == ['ready', 'ready']
        by_name = {item['filename']: item for item in files}
        assert by_name['图表.PNG']['image_metadata'] == {'format': 'PNG', 'mime': 'image/png', 'width': 24, 'height': 16}
        assert by_name['截图.jpeg']['image_metadata']['format'] == 'JPEG'
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '比较这两张图的颜色', 'attachment_ids': [item['id'] for item in files]})
        assert response.status_code == 200
        message = response.json()['messages'][-1]
        assert {item['attachment_id'] for item in message['citations']} == {item['id'] for item in files}
        assert '[出处未核验]' in message['content']
        assert all(item['location'] == '图片本身' for item in message['citations'])
        for name, raw in [('图表.PNG', png), ('截图.jpeg', jpeg)]:
            item = by_name[name]
            assert client.get(f'/api/conversations/{conversation}/attachments/{item["id"]}/file').content == raw
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as restarted:
        assert restarted.post(f'/api/conversations/{conversation}/runs', json={'content': '再看这两张图片是什么颜色？'}).status_code == 202
        answer = wait_message(restarted, conversation, 'complete')
        assert answer['citations'] == message['citations']
        assert len(restarted.get('/api/usage').json()) == 3
        assert len(payloads) == 3


def test_visual_gate_is_revision_bound_and_does_not_block_unrelated_chat_or_memory(database_url: str, tmp_path: Path) -> None:
    """仅实际读图受视觉门槛限制，明确事实保存、普通聊天和拒绝读图不受影响。"""
    from tests.memory.test_memory import configure_memory, memory_provider

    received: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """记忆使用真实 Mem0 的受控嵌入；聊天核对是否真的发送了图片。"""
        payload = json.loads(request.content)
        received.append(payload)
        if request.url.path.endswith('/embeddings'):
            return memory_provider(request)
        if payload.get('tools'):
            return memory_provider(request)
        if len(payload['messages']) == 1 and isinstance(payload['messages'][0]['content'], list):
            return model_response(payload, 'red')
        if payload['messages'][-1]['content'] in ('你好', '不要读取图片，只讲个无关的笑话', '你支持图片功能吗'):
            assert not any(isinstance(message['content'], list) for message in payload['messages'])
            return model_response(payload, '普通聊天回答。')
        return model_response(payload, '已看到图片。[A1]')

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure_memory(client)
        image = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('事实.png', image_bytes())}).json()[0]
        saved = client.post(f'/api/conversations/{conversation}/messages', json={'content': '请记住：我喜欢中文回答', 'attachment_ids': [image['id']]})
        assert saved.status_code == 200
        assert saved.json()['messages'][-1]['memory_action']['status'] == 'complete'
        facts = [item['fact'] for item in client.get('/api/memories').json()]
        assert len(facts) == 1 and ('中文' in facts[0] or 'Chinese' in facts[0])
        for content in ('你好', '不要读取图片，只讲个无关的笑话', '你支持图片功能吗'):
            normal = client.post(f'/api/conversations/{conversation}/messages', json={'content': content})
            assert normal.status_code == 200
            assert normal.json()['messages'][-1]['citations'] == []
        count = len(received)
        started = client.post(f'/api/conversations/{conversation}/runs', json={'content': '看看这张图片'})
        assert started.status_code == 202
        failed = wait_message(client, conversation, 'failed')
        assert '图片能力尚未验证通过' in failed['error']
        assert len(received) == count
        assert client.post('/api/settings/capabilities/image').json()['status'] == 'passed'
        assert client.post(f'/api/conversations/{conversation}/messages/{failed["id"]}/retry').status_code == 202
        answer = wait_message(client, conversation, 'complete')
        assert answer['citations'][0]['attachment_id'] == image['id']
        assert answer['reply_to'] == failed['reply_to']
        client.put('/api/settings', json={'base_url': 'https://chat.example/v1', 'model': 'new-chat', 'api_key': 'test-key'})
        count = len(received)
        assert client.post(f'/api/conversations/{conversation}/runs', json={'content': '再看这张图片'}).status_code == 202
        changed = wait_message(client, conversation, 'failed')
        assert '图片能力尚未验证通过' in changed['error']
        assert len(received) == count


def test_corrupt_disguised_animated_and_oversized_images_are_not_available(database_url: str, tmp_path: Path) -> None:
    """完整性与真实格式检查拒绝假图、PNG/JPEG 截断、多帧和超限，不把失败显示为可理解。"""
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
    with TestClient(create_app(database_url, tmp_path), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        for filename, raw in cases:
            response = client.post(f'/api/conversations/{conversation}/attachments', files={'files': (filename, raw)})
            assert response.status_code == 201
            attachment = next(item for item in client.get(f'/api/conversations/{conversation}/attachments').json() if item['id'] == response.json()[0]['id'])
            assert attachment['status'] == 'failed', filename
            assert attachment['image_metadata'] is None
            assert attachment['error']
            assert client.post(f'/api/conversations/{conversation}/runs', json={'content': '请看这张图', 'attachment_ids': [attachment['id']]}).status_code == 409
        assert client.get('/api/usage').json() == []
        assert client.get(f'/api/conversations/{conversation}').json()['messages'] == []
        many = client.post(f'/api/conversations/{conversation}/attachments', files=[('files', (f'{index}.png', png)) for index in range(6)])
        assert many.status_code == 422
        large = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('oversize.jpg', image_bytes('JPEG') + b'0' * 20_000_001)})
        assert large.status_code == 413


def test_image_sources_are_conversation_owned_and_unbound_uploads_are_private(database_url: str, tmp_path: Path) -> None:
    """未随消息发送的图片和其他会话图片不会外发，同图重传建立独立来源。"""
    payloads: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """从实际多模态材料生成对应引用，没有图片材料时保持普通聊天。"""
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payload['messages']) == 1 and isinstance(payload['messages'][0]['content'], list):
            return model_response(payload, 'red')
        if any(isinstance(message['content'], list) for message in payload['messages']):
            sources = json.loads(payload['messages'][0]['content'].split('\n', 1)[1])
            return model_response(payload, f'红色图片。[{sources[0]["key"]}]')
        return model_response(payload, '没有提供可读取图片。')

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        first = configure(client)
        second = client.post('/api/conversations', json={}).json()['id']
        client.post('/api/settings/capabilities/image')
        raw = image_bytes()
        image = client.post(f'/api/conversations/{first}/attachments', files={'files': ('private.png', raw)}).json()[0]
        assert client.post(f'/api/conversations/{first}/messages', json={'content': '看看图片'}).status_code == 200
        assert not any(isinstance(message['content'], list) for message in payloads[-1]['messages'])
        count = len(payloads)
        assert client.post(f'/api/conversations/{second}/messages', json={'content': '看看图片', 'attachment_ids': [image['id']]}).status_code == 409
        assert len(payloads) == count
        assert client.get(f'/api/conversations/{second}/attachments/{image["id"]}/file').status_code == 404
        original = client.post(f'/api/conversations/{first}/messages', json={'content': '看看图片', 'attachment_ids': [image['id']]}).json()['messages'][-1]
        assert original['citations'][0]['attachment_id'] == image['id']
        assert client.post(f'/api/conversations/{second}/messages', json={'content': '看看刚才图片'}).status_code == 200
        assert not any(isinstance(message['content'], list) for message in payloads[-1]['messages'])
        other = client.post(f'/api/conversations/{second}/attachments', files={'files': ('private.png', raw)}).json()[0]
        assert other['id'] != image['id']
        copied = client.post(f'/api/conversations/{second}/messages', json={'content': '看看图片', 'attachment_ids': [other['id']]}).json()['messages'][-1]
        assert copied['citations'][0]['attachment_id'] == other['id']
        assert copied['citations'][0]['key'] != original['citations'][0]['key']
        assert client.get('/api/memories').json() == []


