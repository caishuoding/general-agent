"""验证图片上传 DeepSeek 后按 file_id 进入会话上下文，TTL 固定 30 天。"""

import time
from email.parser import BytesParser
from email.policy import default
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
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
    """读取模型实际收到的扁平 file 内容块，确保没有内联图片。"""
    parts = [part for message in payload['messages'] if isinstance(message['content'], list) for part in message['content']]
    assert not any(part['type'] == 'image_url' for part in parts)
    return [part['file_id'] for part in parts if part['type'] == 'file']


def upload_response(request: httpx.Request, identifier: str, expected: bytes | None = None) -> httpx.Response:
    """校验真实 multipart 原图、用途和 30 天 TTL，返回官方文件元信息形状。"""
    assert request.method == 'POST' and request.url.path == '/files'
    assert request.headers['Authorization'] == 'Bearer test-key'
    mime = BytesParser(policy=default).parsebytes(
        b'Content-Type: ' + request.headers['Content-Type'].encode() + b'\r\n\r\n' + request.content,
    )
    fields = {part.get_param('name', header='content-disposition'): part for part in mime.iter_parts()}
    assert fields['purpose'].get_payload(decode=True) == b'user_data'
    assert fields['expires_after[anchor]'].get_payload(decode=True) == b'created_at'
    assert fields['expires_after[seconds]'].get_payload(decode=True) == b'2592000'
    if expected is not None:
        assert fields['file'].get_payload(decode=True) == expected
    now = int(time.time())
    return httpx.Response(200, json={'id': identifier, 'object': 'file', 'purpose': 'user_data',
                                    'created_at': now, 'expires_at': now + 2592000})


@pytest.mark.parametrize('content', [
    '请记住这张图片里的信息',
    '先看这张图，再修改记忆中的颜色',
    '查找长期记忆并比较这张图片',
    '图片上写的“忘记”两个字是什么颜色？',
])
@pytest.mark.parametrize('streaming', [False, True])
def test_message_keywords_do_not_hide_image_or_tools(
    database_url: str, tmp_path: Path, content: str, streaming: bool,
) -> None:
    """同步与流式回答均发送有效图片和完整工具，不按用户消息关键词预先筛选。"""
    payloads: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """上传受控图片，并记录模型实际收到的消息和可用工具。"""
        if request.url.path == '/files':
            return upload_response(request, 'file-api-keyword-regression')
        payload = json.loads(request.content)
        payloads.append(payload)
        return model_response(payload, '已查看图片，按当前请求回答。')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('red.png', image_bytes())})
        assert uploaded.status_code == 201
        attachments = client.get(f'/api/conversations/{conversation}/attachments').json()
        assert attachments[0]['status'] == 'ready', attachments[0]['error']
        endpoint = 'runs' if streaming else 'messages'
        response = client.post(f'/api/conversations/{conversation}/{endpoint}', json={'content': content})
        assert response.status_code == (202 if streaming else 200)
        if streaming:
            assert wait_message(client, conversation, 'complete')['content'] == '已查看图片，按当前请求回答。'
        assert len(payloads) == 1
        assert image_parts(payloads[0]) == ['file-api-keyword-regression']
        assert payloads[0]['messages'][-1] == {'role': 'user', 'content': content}
        assert {tool['function']['name'] for tool in payloads[0]['tools']} == {
            'add_memory', 'search_memories', 'update_memory', 'delete_memory',
            'search_web', 'fetch_web', 'search_documents', 'read_document',
        }


def test_png_jpeg_use_file_ids_and_survive_restart(database_url: str, tmp_path: Path) -> None:
    """PNG/JPEG 上传一次后仅用 file_id 回答，重启和追问不重复上传。"""
    png, jpeg = image_bytes(), image_bytes('JPEG')
    uploads: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """核对 multipart 上传和真实 file_id 负载，不依赖 OCR 或引用白名单。"""
        if request.url.path == '/files':
            identifier = 'file-api-' + str(len(uploads) + 1)
            expected = png if len(uploads) == 0 else jpeg
            uploads.append(identifier)
            return upload_response(request, identifier, expected)
        payload = json.loads(request.content)
        assert set(image_parts(payload)) == {'file-api-1', 'file-api-2'}
        assert 'base64,' not in request.content.decode()
        return model_response(payload, '两张图片都是红色。')

    transport = httpx.MockTransport(provider)
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, transport) as client:
        conversation = configure(client)
        uploaded = client.post('/api/conversations/{}/attachments'.format(conversation), files=[('files', ('图表.PNG', png, 'text/plain')), ('files', ('截图.jpeg', jpeg, 'image/jpeg'))])
        assert uploaded.status_code == 201
        files = client.get(f'/api/conversations/{conversation}/attachments').json()
        assert [item['status'] for item in files] == ['ready', 'ready'], [item['error'] for item in files]
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
        assert len(uploads) == 2


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
    uploads: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """给不同会话分别上传，再根据实际 file_id 材料返回回答。"""
        if request.url.path == '/files':
            identifier = 'file-api-' + str(len(uploads) + 1)
            uploads.append(identifier)
            return upload_response(request, identifier, raw)
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
        assert len(uploads) == 2
        assert client.post(f'/api/conversations/{second}/messages', json={'content': '看看图片'}).status_code == 200
        assert image_parts(payloads[-1])
        assert client.get('/api/memories').json() == []


def test_upload_failure_or_missing_ttl_is_not_ready(database_url: str, tmp_path: Path) -> None:
    """上传失败、缺少 TTL 或返回过长 TTL 时不标记图片可用，也不回显供应商错误正文。"""
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """依次返回 HTTP 错误、无有效期文件和超过 30 天有效期的响应。"""
        nonlocal attempts
        attempts += 1
        assert request.url.path == '/files'
        if attempts == 1:
            return httpx.Response(401, json={'error': {'message': 'private-provider-detail'}})
        if attempts == 2:
            return httpx.Response(200, json={'id': 'file-api-missing-ttl', 'created_at': int(time.time())})
        return httpx.Response(200, json={'id': 'file-api-too-long', 'created_at': int(time.time()),
                                        'expires_at': int(time.time()) + 5184000})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        for index in range(3):
            result = client.post(f'/api/conversations/{conversation}/attachments', files={'files': (f'{index}.png', image_bytes())})
            assert result.status_code == 201
        rows = client.get(f'/api/conversations/{conversation}/attachments').json()
        assert [row['status'] for row in rows] == ['failed'] * 3
        assert 'HTTP 401' in rows[0]['error']
        assert '过期时间' in rows[1]['error'] and '30 天 TTL' in rows[2]['error']
        assert 'private-provider-detail' not in json.dumps(rows)
        assert client.get('/api/usage').json() == []
        assert attempts == 3


def test_expired_file_id_requires_new_upload(database_url: str, tmp_path: Path) -> None:
    """本地记录显示图片过期时阻止引用，不自动续期或重复发送原图。"""
    import psycopg

    uploads = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """只允许一次上传，过期后的消息不应发送给模型。"""
        nonlocal uploads
        assert request.url.path == '/files'
        uploads += 1
        return upload_response(request, 'file-api-expiring')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        image = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('expired.png', image_bytes())}).json()[0]
        with psycopg.connect(database_url) as connection:
            connection.execute("UPDATE attachments SET provider_file=jsonb_set(provider_file,'{expires_at}',to_jsonb(%s::bigint)) WHERE id=%s",
                               (int(time.time()) - 1, image['id']))
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取图片'})
        assert response.status_code == 502
        message = client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
        assert '有效期已结束' in message['error']
        row = client.get(f'/api/conversations/{conversation}/attachments').json()[0]
        assert row['status'] == 'failed' and uploads == 1


def test_remote_delete_failure_is_retryable(database_url: str, tmp_path: Path) -> None:
    """远端删除失败保留文件身份及本机原图，重试同一删除操作后完整清理。"""
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """先上传原图，首次删除失败，第二次删除成功。"""
        nonlocal attempts
        if request.method == 'POST':
            return upload_response(request, 'file-api-delete')
        assert request.method == 'DELETE' and request.url.path == '/files/file-api-delete'
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={'error': {'message': 'temporary'}})
        return httpx.Response(200, json={'id': 'file-api-delete', 'object': 'file', 'deleted': True})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        image = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('delete.png', image_bytes())}).json()[0]
        path = f'/api/conversations/{conversation}/attachments/{image["id"]}'
        preview = client.get(path + '/deletion-preview').json()
        failed = client.request('DELETE', path, json={'preview_revision': preview['revision']})
        assert failed.status_code == 503 and failed.json()['phase'] == 'files'
        assert (tmp_path / 'attachments' / image['id']).exists()
        assert client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status'] == 'deleting'
        retried = client.post('/api/source-deletions/' + failed.json()['id'] + '/retry')
        assert retried.json()['status'] == 'complete' and attempts == 2
        assert not (tmp_path / 'attachments' / image['id']).exists()
        assert client.get(f'/api/conversations/{conversation}/attachments').json() == []


def test_deleting_while_uploading_cleans_late_remote_file(database_url: str, tmp_path: Path) -> None:
    """图片上传中删除先停用来源，迟到的文件身份仍被同一删除流程清理。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic, sleep

    entered, release = Event(), Event()
    deleted: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """暂停远端上传响应，让删除与上传结果形成确定的交错。"""
        if request.method == 'POST':
            response = upload_response(request, 'file-api-late')
            entered.set()
            assert release.wait(10)
            return response
        deleted.append(request.url.path)
        return httpx.Response(200, json={'id': 'file-api-late', 'deleted': True})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        with ThreadPoolExecutor() as pool:
            upload = pool.submit(client.post, f'/api/conversations/{conversation}/attachments', files={'files': ('late.png', image_bytes())})
            try:
                assert entered.wait(5)
                image = client.get(f'/api/conversations/{conversation}/attachments').json()[0]
                path = f'/api/conversations/{conversation}/attachments/{image["id"]}'
                preview = client.get(path + '/deletion-preview').json()
                deletion = pool.submit(client.request, 'DELETE', path, json={'preview_revision': preview['revision']})
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    if client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status'] == 'deleting':
                        break
                    sleep(.02)
                else:
                    raise AssertionError('删除未先停用附件。')
            finally:
                release.set()
            assert upload.result(timeout=5).status_code == 201
            assert deletion.result(timeout=5).json()['status'] == 'complete'
        assert deleted == ['/files/file-api-late']
        assert client.get(f'/api/conversations/{conversation}/attachments').json() == []


def test_legacy_image_uploads_once_on_first_use(database_url: str, tmp_path: Path) -> None:
    """旧版本只有本机原图的附件在首次读取时补上传，之后复用新的 file_id。"""
    import psycopg

    uploads = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """区分初始上传与旧记录补上传，聊天只接受最新文件身份。"""
        nonlocal uploads
        if request.url.path == '/files':
            uploads += 1
            return upload_response(request, f'file-api-legacy-{uploads}')
        payload = json.loads(request.content)
        assert image_parts(payload) == ['file-api-legacy-2']
        return model_response(payload, '红色。')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        image = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('old.png', image_bytes())}).json()[0]
        with psycopg.connect(database_url) as connection:
            connection.execute('UPDATE attachments SET provider_file=NULL WHERE id=%s', (image['id'],))
        for content in ('读取旧图片', '再次查看图片'):
            response = client.post(f'/api/conversations/{conversation}/messages', json={'content': content})
            assert response.status_code == 200
        assert uploads == 2


def test_new_service_does_not_receive_previous_file_id(database_url: str, tmp_path: Path) -> None:
    """LLM 服务地址变化时要求重传，避免向新服务发送旧 file_id。"""
    from tests.auth.support import set_global_settings

    def provider(request: httpx.Request) -> httpx.Response:
        """只允许旧服务上传，任何向新服务发送图片的请求都应失败。"""
        assert request.url.host == 'model.example' and request.url.path == '/files'
        return upload_response(request, 'file-api-original-service')

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('service.png', image_bytes())})
        set_global_settings(database_url, tmp_path, {'base_url': 'https://other.example/v1', 'model': 'new', 'api_key': 'other-key'})
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取图片'})
        assert response.status_code == 502
        message = client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
        assert '服务地址已变化' in message['error']
