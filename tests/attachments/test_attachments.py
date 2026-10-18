"""经真实 PostgreSQL 与外部模型替身验证附件上传、隔离和可信出处。"""

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.conversation.test_streaming import wait_message
def configure(client: TestClient) -> str:
    """配置固定模型并创建用于资料问答的独立会话。"""
    client.put('/api/settings', json={'base_url': 'https://model.example/v1', 'model': 'controlled', 'api_key': 'test-key'})
    return str(client.post('/api/conversations', json={}).json()['id'])


def test_text_upload_stream_citation_and_restart(database_url: str, tmp_path: Path) -> None:
    """上传的 UTF-8 文本经真实流式入口回答，出处与重新进入后的资料均持久化。"""
    requests: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """在模型 HTTP 边界验证原文材料与用户授权分离，并返回可核对引用。"""
        payload = json.loads(request.content)
        requests.append(payload)
        assert '蓝鲸项目于星期五交付' in str(payload['messages'])
        answer = '蓝鲸项目星期五交付。[A1]'
        if payload.get('stream'):
            event = {'choices': [{'delta': {'content': answer}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 30, 'completion_tokens': 8, 'total_tokens': 38}}
            return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={'choices': [{'message': {'content': answer}}], 'usage': {'prompt_tokens': 30, 'completion_tokens': 8, 'total_tokens': 38}})

    transport = httpx.MockTransport(provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('计划.md', '蓝鲸项目于星期五交付。\n负责人是林女士。'.encode(), 'text/markdown')})
        assert uploaded.status_code == 201
        attachment = client.get(f'/api/conversations/{conversation}/attachments').json()[0]
        assert attachment['status'] == 'ready'
        started = client.post(f'/api/conversations/{conversation}/runs', json={'content': '蓝鲸项目什么时候交付？', 'attachment_ids': [attachment['id']]})
        assert started.status_code == 202
        message = wait_message(client, conversation, 'complete')
        assert message['citations'][0]['attachment_id'] == attachment['id']
        assert message['citations'][0]['filename'] == '计划.md'
        assert message['citations'][0]['location'] == '第 1–2 行'
        assert '蓝鲸项目于星期五交付' in message['citations'][0]['excerpt']
        assert requests[0]['messages'][-1] == {'role': 'user', 'content': '蓝鲸项目什么时候交付？'}
        user = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
        assert user['attachment_ids'] == [attachment['id']]
    with TestClient(create_app(database_url, tmp_path, transport), base_url='http://127.0.0.1:8000') as restarted:
        listed = restarted.get(f'/api/conversations/{conversation}/attachments').json()
        assert listed[0]['status'] == 'ready'
        assert restarted.get(f'/api/conversations/{conversation}/attachments/{attachment["id"]}/file').content == '蓝鲸项目于星期五交付。\n负责人是林女士。'.encode()
        response = restarted.post(f'/api/conversations/{conversation}/messages', json={'content': '请再告诉我交付时间'})
        assert response.status_code == 200
        assert response.json()['messages'][-1]['citations'][0]['attachment_id'] == attachment['id']
        assert len(restarted.get('/api/usage').json()) == 2


def test_fabricated_sources_are_not_presented_as_verified(database_url: str, tmp_path: Path) -> None:
    """模型引用未提供的片段时不生成可信出处，并明确告知答案缺少核验依据。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """返回模型编造的来源标识，保持外部响应本身成功。"""
        return httpx.Response(200, json={'choices': [{'message': {'content': '结论来自第九百页。[A999]'}}]})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        attachment = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('原文.txt', b'Only one line.', 'text/plain')}).json()[0]
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '资料讲了什么？', 'attachment_ids': [attachment['id']]})
        message = response.json()['messages'][-1]
        assert message['citations'] == []
        assert '[A999]' not in message['content']
        assert '缺少可核验的资料引用' in message['content']


def test_isolation_unbound_upload_and_independent_copies(database_url: str, tmp_path: Path) -> None:
    """未发送的上传和其他会话文件不进入模型，同内容重传保留独立身份及下载范围。"""
    payloads: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """收集外部模型真正收到的材料，用独立字面事实检查会话隔离。"""
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '测试回答。[A1]'}}]})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        first = configure(client)
        second = client.post('/api/conversations', json={}).json()['id']
        original = b'Private launch code: AZURE-1289.'
        a = client.post(f'/api/conversations/{first}/attachments', files={'files': ('../机密.txt', original)}).json()[0]
        assert a['filename'] == '机密.txt'
        client.post(f'/api/conversations/{first}/messages', json={'content': '普通问候'})
        assert 'AZURE-1289' not in str(payloads[-1])
        assert client.post(f'/api/conversations/{first}/messages', json={'content': '资料问题', 'attachment_ids': [a['id']]}).status_code == 200
        assert 'AZURE-1289' in str(payloads[-1])
        assert client.get(f'/api/conversations/{second}/attachments/{a["id"]}/file').status_code == 404
        rejected = client.post(f'/api/conversations/{second}/runs', json={'content': '越界读取', 'attachment_ids': [a['id']]})
        assert rejected.status_code == 409
        assert len(payloads) == 2
        client.post(f'/api/conversations/{second}/messages', json={'content': '新会话的问题'})
        assert 'AZURE-1289' not in str(payloads[-1])
        assert client.get(f'/api/conversations/{second}/attachments').json() == []
        b = client.post(f'/api/conversations/{second}/attachments', files={'files': ('机密.txt', original)}).json()[0]
        c = client.post(f'/api/conversations/{second}/attachments', files={'files': ('机密.txt', original)}).json()[0]
        assert len({a['id'], b['id'], c['id']}) == 3
        for attachment in (b, c):
            assert client.get(f'/api/conversations/{second}/attachments/{attachment["id"]}/file').content == original
        assert client.post(f'/api/conversations/{second}/messages', json={'content': '资料问题', 'attachment_ids': [b['id']]}).status_code == 200
        assert 'AZURE-1289' in str(payloads[-1])
        assert c['id'] not in str(payloads[-1])


def test_source_keys_do_not_change_meaning_between_questions(database_url: str, tmp_path: Path) -> None:
    """两轮检索改变片段顺序时，同一个引用标识仍指向同一段原文。"""
    reference: str | None = None

    def provider(request: httpx.Request) -> httpx.Response:
        """第二轮核对先前来源标识不被重新分配给不同文件或位置。"""
        nonlocal reference
        messages = json.loads(request.content)['messages']
        sources = json.loads(messages[0]['content'].split('\n', 1)[1])
        if reference is None:
            source = next(item for item in sources if 'ORCHID' in item['excerpt'])
            reference = source['key']
        else:
            same_key = next(item for item in sources if item['key'] == reference)
            assert 'ORCHID' in same_key['excerpt']
        return httpx.Response(200, json={'choices': [{'message': {'content': f'原始证据。[{reference}]'}}]})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        files = client.post(f'/api/conversations/{conversation}/attachments', files=[('files', ('first.txt', b'ORCHID delivery on Friday.')), ('files', ('second.txt', b'CEDAR delivery on Monday.'))]).json()
        first = client.post(f'/api/conversations/{conversation}/messages', json={'content': 'ORCHID delivery?', 'attachment_ids': [item['id'] for item in files]})
        assert first.status_code == 200
        second = client.post(f'/api/conversations/{conversation}/messages', json={'content': 'CEDAR delivery?'})
        assert second.status_code == 200
        assert second.json()['messages'][-1]['citations'][0]['attachment_id'] == files[0]['id']


def test_upload_counts_and_message_count_cannot_be_bypassed(database_url: str, tmp_path: Path) -> None:
    """上传 4/5/6 个与分批后绑定 6 个、重复 ID 的边界均由后端执行。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """只对合法消息返回固定可计量结果。"""
        return httpx.Response(200, json={'choices': [{'message': {'content': '资料回答。[A1]'}}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        for count in (4, 5, 6):
            previous = len(client.get(f'/api/conversations/{conversation}/attachments').json())
            upload = client.post(f'/api/conversations/{conversation}/attachments', files=[('files', (f'{index}.txt', b'Known text.')) for index in range(count)])
            assert upload.status_code == (422 if count == 6 else 201)
            assert len(client.get(f'/api/conversations/{conversation}/attachments').json()) == previous + (0 if count == 6 else count)
        ids = [row['id'] for row in client.get(f'/api/conversations/{conversation}/attachments').json()]
        too_many = client.post(f'/api/conversations/{conversation}/runs', json={'content': '资料问题', 'attachment_ids': ids[:6]})
        assert too_many.status_code == 422
        assert "最多五个" in too_many.json()["detail"]
        duplicate = client.post(f'/api/conversations/{conversation}/runs', json={'content': '资料问题', 'attachment_ids': [ids[0], ids[0]]})
        assert duplicate.status_code == 409
        assert client.get('/api/usage').json() == []
        assert client.get(f'/api/conversations/{conversation}').json()['messages'] == []
        assert client.post(f'/api/conversations/{conversation}/messages', json={'content': '资料问题', 'attachment_ids': ids[:5]}).status_code == 200
        assert len(client.get(f'/api/conversations/{conversation}').json()['messages'][0]['attachment_ids']) == 5


def test_decoding_failure_and_unsupported_types_are_truthful(database_url: str, tmp_path: Path) -> None:
    """空文字、非 UTF-8 和伪装二进制保留失败状态；未支持类型明确拒绝。"""
    with TestClient(create_app(database_url, tmp_path), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        for name, body in [('empty.txt', b'  \n\t'), ('bad.txt', b'\xff\xfe'), ('binary.md', b'hello\x00world')]:
            upload = client.post(f'/api/conversations/{conversation}/attachments', files={'files': (name, body)})
            assert upload.status_code == 201
            identifier = upload.json()[0]['id']
            rows = client.get(f'/api/conversations/{conversation}/attachments').json()
            row = next(item for item in rows if item['id'] == identifier)
            assert row['status'] == 'failed'
            assert row['error']
            assert client.post(f'/api/conversations/{conversation}/runs', json={'content': '请阅读', 'attachment_ids': [identifier]}).status_code == 409
        for name in ('sheet.xlsx', 'audio.mp3', 'video.mp4', 'unknown.exe'):
            result = client.post(f'/api/conversations/{conversation}/attachments', files={'files': (name, b'plain text')})
            assert result.status_code == 422
            assert '暂未启用' in result.json()['detail']
        bom = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('bom.TXT', b'\xef\xbb\xbfhello\r\nworld')})
        assert bom.status_code == 201
        assert client.get(f'/api/conversations/{conversation}/attachments').json()[-1]['status'] == 'ready'
        assert client.get('/api/usage').json() == []
        assert client.get(f'/api/conversations/{conversation}').json()['messages'] == []


def test_actual_byte_boundary_and_truncated_upload_cleanup(database_url: str, tmp_path: Path) -> None:
    """19,999,999/20,000,000 字节可用，超过一字节和截断请求均拒绝且无半成品。"""
    with TestClient(create_app(database_url, tmp_path), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        for size in (19_999_999, 20_000_000, 20_000_001):
            result = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('large.txt', b'a' * size)})
            assert result.status_code == (413 if size > 20_000_000 else 201)
            if result.status_code == 201:
                item = result.json()[0]
                assert item['size'] == size
                assert len(client.get(f'/api/conversations/{conversation}/attachments/{item["id"]}/file').content) == size
        prior = client.get(f'/api/conversations/{conversation}/attachments').json()
        assert len(prior) == 2
        assert all(row['status'] == 'ready' for row in prior)
        header = b'--small\r\nContent-Disposition: form-data; name="files"; filename="cut.txt"\r\n\r\n'
        malformed = client.post(f'/api/conversations/{conversation}/attachments', content=header + b'not completed', headers={'Content-Type': 'multipart/form-data; boundary=small'})
        assert malformed.status_code == 422
        assert '未完整结束' in malformed.json()['detail']
        assert client.get(f'/api/conversations/{conversation}/attachments').json() == prior
        assert list((tmp_path / 'attachments').glob('*.upload')) == []
        payload = header + b'hello' + b'\r\n--small--\r\n'
        tiny = client.post(f'/api/conversations/{conversation}/attachments', content=(payload[index:index + 1] for index in range(len(payload))), headers={'Content-Type': 'multipart/form-data; boundary=small'})
        assert tiny.status_code == 201
        assert tiny.json()[0]['size'] == 5
        assert client.get('/api/usage').json() == []




def test_specific_question_can_find_text_near_document_end(database_url: str, tmp_path: Path) -> None:
    """中文词组位于提问奇数偏移及资料末尾时仍可检索，并保留真实行内位置。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """只允许实际收到文末证据时返回答案及该证据的后端标识。"""
        messages = json.loads(request.content)['messages']
        sources = json.loads(messages[0]['content'].split('\n', 1)[1])
        evidence = next(item for item in sources if '玄武口令为海风' in item['excerpt'])
        assert evidence['location'].startswith('第 1 行，字符 ')
        assert len(sources) <= 12
        return httpx.Response(200, json={'choices': [{'message': {'content': f'口令是海风。[{evidence["key"]}]'}}]})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('long.txt', ('普通背景。' * 5000 + '玄武口令为海风。').encode())}).json()[0]
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '问玄武？', 'attachment_ids': [uploaded['id']]})
        assert response.status_code == 200
        assert '玄武口令为海风' in response.json()['messages'][-1]['citations'][0]['excerpt']


def test_retry_retains_original_attachment_and_stop_freezes_text(database_url: str, tmp_path: Path) -> None:
    """失败重试保留原消息文件关系，停止后即使出现来源提示也不能再改写已见文字。"""
    from collections.abc import Iterator
    from threading import Event
    from tests.conversation.test_streaming import ControlledStream

    release = Event()
    calls = 0

    class SourceStream(httpx.SyncByteStream):
        """把资料流停在未完成引用前，模拟用户在生成中停止。"""

        def __iter__(self) -> Iterator[bytes]:
            """先输出无引用片段，收到释放信号后才发送终止标志与用量。"""
            yield b'data: {"choices":[{"delta":{"content":"partial source"}}]}\n\n'
            assert release.wait(10)
            yield b'data: {"choices":[{"delta":{"content":" [A1]"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
            yield b'data: [DONE]\n\n'

    def provider(request: httpx.Request) -> httpx.Response:
        """每次真实请求都带原始资料，首轮失败、第二轮允许停止。"""
        nonlocal calls
        calls += 1
        assert 'retry-source-only' in str(json.loads(request.content)['messages'])
        if calls == 1:
            released = Event()
            released.set()
            return httpx.Response(200, stream=ControlledStream(released, interrupted=True))
        return httpx.Response(200, stream=SourceStream())

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        attachment = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('retry.txt', b'retry-source-only')}).json()[0]
        client.post(f'/api/conversations/{conversation}/runs', json={'content': '资料问题', 'attachment_ids': [attachment['id']]})
        failed = wait_message(client, conversation, 'failed')
        try:
            assert client.post(f'/api/conversations/{conversation}/messages/{failed["id"]}/retry').status_code == 202
            partial = wait_message(client, conversation, 'generating', 'partial source')
            assert client.post(f'/api/conversations/{conversation}/messages/{partial["id"]}/stop').status_code == 200
        finally:
            release.set()
        wait_message(client, conversation, 'stopped', 'partial source')
        details = client.get(f'/api/conversations/{conversation}').json()
        assert len(details['messages']) == 3
        assert details['messages'][0]['attachment_ids'] == [attachment['id']]
        assert details['messages'][-1]['reply_to'] == failed['reply_to']
    with TestClient(create_app(database_url, tmp_path), base_url='http://127.0.0.1:8000') as restarted:
        wait_message(restarted, conversation, 'stopped', 'partial source')
        assert len(restarted.get('/api/usage').json()) == 2
