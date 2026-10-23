"""通过真实对话工具循环和模型 HTTP 边界验证知识库召回。"""

import json
import time
from pathlib import Path
from threading import Event
from typing import Any

import httpx

from general_agent.infrastructure.settings import EmbeddingSettingsStore, StoredEmbeddingSettings
from tests.auth.support import authenticated_client
from tests.attachments.test_attachments import configure_settings
from tests.attachments.test_documents import image_pdf_bytes
from tests.attachments.test_document_images import image_files_response
from tests.knowledge.test_knowledge_base import wait_document


def tool_response(base: str, query: str = 'car') -> httpx.Response:
    """返回模型真实协议中的知识库函数调用，不注入文件 ID。"""
    return httpx.Response(200, json={'choices': [{'message': {'content': None, 'tool_calls': [
        {'id': 'knowledge-call', 'type': 'function', 'function': {'name': 'search_knowledge',
         'arguments': json.dumps({'knowledge_base_id': base, 'query': query})}},
    ]}}]})


def test_agent_searches_selected_base_and_refuses_unselected_base(database_url: str, tmp_path: Path) -> None:
    """模型只凭知识库 ID 和 query 检索库内资料，伪造未选择的库会被拒绝。"""
    requested: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    sent: list[list[str]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """提供受控嵌入与工具调用，记录实际外发内容和工具结果。"""
        payload = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            sent.append(payload['input'])
            return httpx.Response(200, json={'model': 'test-embedding', 'data': [
                {'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(payload['input'])],
                'usage': {'prompt_tokens': 4, 'total_tokens': 4}})
        tool_messages = [item for item in payload['messages'] if item['role'] == 'tool']
        if not tool_messages:
            definitions = {item['function']['name']: item['function'] for item in payload['tools']}
            assert set(definitions['search_knowledge']['parameters']['properties']) == {'knowledge_base_id', 'query'}
            return tool_response(requested['base'])
        results.append(json.loads(tool_messages[-1]['content']))
        return httpx.Response(200, json={'choices': [{'message': {'content': '根据知识库资料作答。'}}]})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='test-embedding', api_key='test-key', dimensions=2, revision='v1'))
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '车辆资料'}).json()['id']
        other = client.post('/api/knowledge-bases', json={'name': '未选资料'}).json()['id']
        for identifier, body in ((base, b'vehicle maintenance'), (other, b'PRIVATE_OTHER_BASE')):
            uploaded = client.post(f'/api/knowledge-bases/{identifier}/documents', files={'files': ('manual.txt', body)}).json()[0]
            assert wait_document(client, identifier, uploaded['id'])['warning'] is None
        conversation = client.post('/api/conversations', json={}).json()['id']
        client.put(f'/api/conversations/{conversation}/knowledge-bases', json={'knowledge_base_ids': [base]})
        sent.clear()
        requested['base'] = other
        assert client.post(f'/api/conversations/{conversation}/messages', json={'content': '查一下车辆维护'}).status_code == 200
        assert 'error' in results[-1]
        assert not sent, '未选择知识库不能参加召回或发出嵌入请求'
        requested['base'] = base
        assert client.post(f'/api/conversations/{conversation}/messages', json={'content': '再查一次车辆维护'}).status_code == 200
        assert results[-1]['results'][0]['content'] == 'vehicle maintenance'
        assert results[-1]['retrieval']['mode'] == 'hybrid'
        assert sent == [['car']], '知识库查询不能重新嵌入整份文档'
        assert any(item['purpose'] == 'knowledge_ingestion' and item['unit'] == 'embedding_tokens' for item in client.get('/api/usage').json())


def test_image_progress_is_observable_and_original_reaches_next_model_request(database_url: str, tmp_path: Path) -> None:
    """纯文字描述原样入库且进度可见，图片切片命中后下一轮实际携带原图。"""
    entered, release = Event(), Event()
    base_holder: dict[str, str] = {}
    saw_original: list[bool] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """阻塞纯文字视觉响应以观察进度，再核验描述原文和正确的原图身份。"""
        if request.url.path == '/files':
            return image_files_response()
        if request.method == 'DELETE':
            return httpx.Response(200)
        payload = json.loads(request.content)
        if not payload.get('stream'):
            entered.set()
            assert release.wait(15), '视觉测试没有释放响应'
            description = 'ORANGE_CHART：橙色图表，季度增长柱状图。'
            return httpx.Response(200, json={'choices': [{'message': {'content': description}}],
                                           'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}})
        tools = [item for item in payload['messages'] if item['role'] == 'tool']
        if not tools:
            return tool_response(base_holder['id'], 'ORANGE_CHART')
        result = json.loads(tools[-1]['content'])
        assert result['results'][0]['image_id']
        assert result['results'][0]['content'] == 'ORANGE_CHART：橙色图表，季度增长柱状图。'
        parts = [part for item in payload['messages'] if isinstance(item.get('content'), list) for part in item['content']]
        saw_original.append({'type': 'file', 'file_id': 'file-api-word-chart'} in parts)
        return httpx.Response(200, json={'choices': [{'message': {'content': '图表说明季度增长。'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '图表资料'}).json()['id']
        base_holder['id'] = base
        try:
            uploaded = client.post(f'/api/knowledge-bases/{base}/documents', files={'files': ('chart.pdf', image_pdf_bytes())}).json()[0]
            assert entered.wait(10), '图片未进入视觉处理阶段'
            progress = client.get(f'/api/knowledge-bases/{base}/documents').json()[0]
            assert progress['status'] == 'processing'
            assert (progress['stage'], progress['completed'], progress['total']) == ('images', 0, 1)
        finally:
            release.set()
        assert wait_document(client, base, uploaded['id'])['status'] == 'ready'
        conversation = client.post('/api/conversations', json={}).json()['id']
        client.put(f'/api/conversations/{conversation}/knowledge-bases', json={'knowledge_base_ids': [base]})
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '图表说明什么？'})
        assert response.status_code == 200, response.text
        assert saw_original == [True]


def test_failed_index_retries_without_reembedding_completed_batches(database_url: str, tmp_path: Path) -> None:
    """索引第二批失败后保持关键词可用，重试只发送缺少向量的切片。"""
    batches: list[list[str]] = []
    fail = True

    def provider(request: httpx.Request) -> httpx.Response:
        """让第二批嵌入失败一次，成功批次的真实用量仍然保留。"""
        nonlocal fail
        payload = json.loads(request.content)
        texts = payload['input']
        batches.append(texts)
        if len(batches) == 2 and fail:
            fail = False
            return httpx.Response(503)
        return httpx.Response(200, json={'model': 'test-embedding', 'data': [
            {'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(texts)]})

    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='test-embedding', api_key='test-key', dimensions=2, revision='v1'))
    body = '\n\n'.join(f'Paragraph {index}. ' + 'alpha value ' * 55 for index in range(30)).encode()
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '重试测试'}).json()['id']
        document = client.post(f'/api/knowledge-bases/{base}/documents', files={'files': ('large.txt', body)}).json()[0]
        first = wait_document(client, base, document['id'])
        assert first['status'] == 'ready' and '向量索引失败' in first['warning']
        assert len(batches) == 2 and len(batches[0]) == 16
        assert client.post(f'/api/knowledge-bases/{base}/documents/{document["id"]}/retry').status_code == 200
        retried = wait_document(client, base, document['id'])
        assert retried['status'] == 'ready' and retried['warning'] is None
        assert all(text not in batch for text in batches[0] for batch in batches[2:])


def test_deleting_during_image_description_does_not_restore_document(database_url: str, tmp_path: Path) -> None:
    """删除处理中的文档立即禁止访问，迟到视觉结果回收远端且不能重新发布。"""
    entered, release = Event(), Event()
    deleted: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """阻塞纯文字描述直到删除发生，再记录迟到结果的远端回收请求。"""
        if request.url.path == '/files':
            return image_files_response()
        if request.method == 'DELETE':
            deleted.append(request.url.path)
            return httpx.Response(200)
        entered.set()
        assert release.wait(15)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'late image'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '删除测试'}).json()['id']
        try:
            document = client.post(f'/api/knowledge-bases/{base}/documents', files={'files': ('scan.pdf', image_pdf_bytes())}).json()[0]
            assert entered.wait(10)
            assert client.delete(f'/api/knowledge-bases/{base}/documents/{document["id"]}').status_code == 200
            assert client.get(f'/api/knowledge-bases/{base}/documents/{document["id"]}/file').status_code == 404
        finally:
            release.set()
        for _ in range(100):
            if not client.get(f'/api/knowledge-bases/{base}/documents').json():
                break
            time.sleep(0.02)
        assert client.get(f'/api/knowledge-bases/{base}/documents').json() == []
        assert deleted == ['/files/file-api-word-chart']


def test_embedding_configuration_change_rebuilds_on_restart(database_url: str, tmp_path: Path) -> None:
    """重启发现嵌入配置变化后从持久原件重建索引，文档身份保持不变。"""
    sent: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """记录两个向量空间各自的建库请求，保证不混用旧版本向量。"""
        payload = json.loads(request.content)
        sent.append(payload['model'])
        return httpx.Response(200, json={'model': payload['model'], 'data': [
            {'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(payload['input'])]})

    settings = EmbeddingSettingsStore(tmp_path)
    settings.write(StoredEmbeddingSettings(base_url='https://embedding.example/v1', model='embedding-v1',
                   api_key='test-key', dimensions=2, revision='v1'))
    transport = httpx.MockTransport(provider)
    with authenticated_client(database_url, tmp_path, transport) as client:
        base = client.post('/api/knowledge-bases', json={'name': '重启测试'}).json()['id']
        document = client.post(f'/api/knowledge-bases/{base}/documents', files={'files': ('source.txt', b'original handbook')}).json()[0]
        assert wait_document(client, base, document['id'])['warning'] is None
    settings.write(StoredEmbeddingSettings(base_url='https://embedding.example/v1', model='embedding-v2',
                   api_key='test-key', dimensions=2, revision='v2'))
    with authenticated_client(database_url, tmp_path, transport) as client:
        assert wait_document(client, base, document['id'])['warning'] is None
        assert client.get(f'/api/knowledge-bases/{base}/documents/{document["id"]}/file').content == b'original handbook'
    assert sent == ['embedding-v1', 'embedding-v2']
