"""通过真实对话工具循环和模型 HTTP 边界验证知识库召回。"""

import json
import time
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

from general_agent.infrastructure.settings import EmbeddingSettingsStore, StoredEmbeddingSettings
from general_agent.knowledge.tools import KnowledgeTools
from tests.auth.support import authenticated_client
from tests.attachments.test_attachments import configure_settings
from tests.attachments.test_documents import image_pdf_bytes
from tests.attachments.test_document_images import image_files_response, word_with_chart
from tests.knowledge.test_knowledge_base import wait_document


def tool_response(query: str = 'car') -> httpx.Response:
    """返回仅含查询词的知识库函数调用，检索范围由后端会话绑定决定。"""
    return httpx.Response(200, json={'choices': [{'message': {'content': None, 'tool_calls': [
        {'id': 'knowledge-call', 'type': 'function', 'function': {'name': 'search_knowledge',
         'arguments': json.dumps({'query': query})}},
    ]}}]})


def test_agent_searches_only_the_session_bound_base_without_receiving_scope(database_url: str, tmp_path: Path) -> None:
    """模型只发送 query；上下文不含库清单，后端固定只召回会话绑定的知识库。"""
    results: list[dict[str, Any]] = []
    sent: list[list[str]] = []
    initial_messages: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """提供受控嵌入与工具调用，核验模型没有获得知识库范围。"""
        payload = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            sent.append(payload['input'])
            return httpx.Response(200, json={'model': 'test-embedding', 'data': [
                {'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(payload['input'])],
                'usage': {'prompt_tokens': 4, 'total_tokens': 4}})
        tool_messages = [item for item in payload['messages'] if item['role'] == 'tool']
        if not tool_messages:
            initial_messages.extend(payload['messages'])
            definitions = {item['function']['name']: item['function'] for item in payload['tools']}
            assert set(definitions['search_knowledge']['parameters']['properties']) == {'query'}
            return tool_response()
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
        assert client.post(f'/api/conversations/{conversation}/messages', json={'content': '查一下车辆维护'}).status_code == 200
        assert results[-1]['results'][0]['content'] == 'vehicle maintenance'
        assert results[-1]['results'][0]['knowledge_base_id'] == base
        assert all(item['knowledge_base_id'] == base for item in results[-1]['results'])
        assert results[-1]['retrieval']['mode'] == 'hybrid'
        assert sent == [['car']], '知识库查询不能重新嵌入整份文档'
        context_text = json.dumps(initial_messages, ensure_ascii=False)
        assert base not in context_text and other not in context_text
        assert '车辆资料' not in context_text and '未选资料' not in context_text
        assert any(item['purpose'] == 'knowledge_ingestion' and item['unit'] == 'embedding_tokens' for item in client.get('/api/usage').json())


def test_all_selected_bases_form_one_fixed_backend_scope(database_url: str, tmp_path: Path) -> None:
    """会话绑定多个知识库时，后端一次固定检索全部库，而不是要求模型逐个选择。"""
    results: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """响应知识库入库嵌入与仅含查询词的工具调用。"""
        payload = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'model': 'test-embedding', 'data': [
                {'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(payload['input'])]})
        tool_messages = [item for item in payload['messages'] if item['role'] == 'tool']
        if not tool_messages:
            return tool_response('maintenance')
        results.append(json.loads(tool_messages[-1]['content']))
        return httpx.Response(200, json={'choices': [{'message': {'content': '已比较两个知识库。'}}]})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='test-embedding', api_key='test-key', dimensions=2, revision='v1'))
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        first = client.post('/api/knowledge-bases', json={'name': '车辆手册'}).json()['id']
        second = client.post('/api/knowledge-bases', json={'name': '维修规范'}).json()['id']
        for identifier, filename, body in ((first, 'vehicle.txt', b'vehicle maintenance'),
                                           (second, 'repair.txt', b'repair maintenance')):
            uploaded = client.post(f'/api/knowledge-bases/{identifier}/documents', files={'files': (filename, body)}).json()[0]
            assert wait_document(client, identifier, uploaded['id'])['warning'] is None
        conversation = client.post('/api/conversations', json={}).json()['id']
        client.put(f'/api/conversations/{conversation}/knowledge-bases',
                   json={'knowledge_base_ids': [first, second]})
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '比较维护规范'})
        assert response.status_code == 200, response.text
        found = results[-1]['results']
        assert {item['knowledge_base_id'] for item in found} == {first, second}
        assert {item['knowledge_base_name'] for item in found} == {'车辆手册', '维修规范'}
        assert {item['content'] for item in found} == {'vehicle maintenance', 'repair maintenance'}


def test_cited_chunks_enter_context_in_source_order_and_persist_only_used_sources(
    database_url: str, tmp_path: Path, monkeypatch: Any,
) -> None:
    """命中切片按原文序号进入上下文，仅回答实际引用的切片持久化为可核对来源。"""
    monkeypatch.setenv('DOCUMENT_CHUNK_TOKENS', '200')
    monkeypatch.setenv('DOCUMENT_CHUNK_OVERFLOW_TOKENS', '0')
    monkeypatch.setenv('DOCUMENT_CHUNK_OVERLAP_TOKENS', '0')
    monkeypatch.setenv('DOCUMENT_SEARCH_RESULTS', '10')
    tool_results: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """返回受控嵌入与知识库工具结果，再按返回编号构造引用回答。"""
        payload = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'model': 'test-embedding', 'data': [
                {'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(payload['input'])]})
        messages = [item for item in payload['messages'] if item['role'] == 'tool']
        if not messages:
            return tool_response('THIRD')
        result = json.loads(messages[-1]['content'])
        tool_results.append(result)
        keys = [item['key'] for item in result['results']]
        return httpx.Response(200, json={'choices': [{'message': {
            'content': '原文先说明范围，后给出结论。' + ''.join(f'[{key}]' for key in (keys[0], keys[-1])),
        }}]})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='test-embedding', api_key='test-key', dimensions=2, revision='v1'))
    original = '\n\n'.join((('FIRST ' * 220).strip(), ('SECOND ' * 220).strip(), ('THIRD ' * 220).strip()))
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '顺序资料'}).json()['id']
        uploaded = client.post(f'/api/knowledge-bases/{base}/documents',
                               files={'files': ('ordered.txt', original.encode())}).json()[0]
        assert wait_document(client, base, uploaded['id'])['warning'] is None
        conversation = client.post('/api/conversations', json={}).json()['id']
        client.put(f'/api/conversations/{conversation}/knowledge-bases', json={'knowledge_base_ids': [base]})
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '查第三段'})
        assert response.status_code == 200, response.text
        found = tool_results[-1]['results']
        assert [item['chunk_id'] for item in found] == sorted(item['chunk_id'] for item in found)
        assert found[0]['content'].startswith('FIRST') and found[-1]['content'].startswith('THIRD')
        message = response.json()['messages'][-1]
        assert [item['key'] for item in message['knowledge_sources']] == [found[0]['key'], found[-1]['key']]
        assert all(item['filename'] == 'ordered.txt' and item['knowledge_base_name'] == '顺序资料'
                   for item in message['knowledge_sources'])
        assert all(item['kind'] == 'text' for item in message['knowledge_sources'])
        assert message['knowledge_sources'][0]['excerpt'].startswith('FIRST')
        assert message['knowledge_sources'][-1]['excerpt'].startswith('THIRD')


def test_unbound_conversation_never_falls_back_to_account_wide_search(database_url: str, tmp_path: Path) -> None:
    """会话未绑定知识库时返回明确状态，不搜索账号下未绑定的资料或生成查询向量。"""
    results: list[dict[str, Any]] = []
    query_embeddings: list[list[str]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """完成知识库入库嵌入，同时记录问答阶段是否错误发起查询嵌入。"""
        payload = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            if any(text.startswith(' QUESTION_ONLY') for text in payload['input']):
                query_embeddings.append(payload['input'])
            return httpx.Response(200, json={'model': 'test-embedding', 'data': [
                {'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(payload['input'])]})
        tool_messages = [item for item in payload['messages'] if item['role'] == 'tool']
        if not tool_messages:
            return tool_response(' QUESTION_ONLY ')
        results.append(json.loads(tool_messages[-1]['content']))
        return httpx.Response(200, json={'choices': [{'message': {'content': '当前会话没有绑定知识库。'}}]})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='test-embedding', api_key='test-key', dimensions=2, revision='v1'))
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '账号私有资料'}).json()['id']
        uploaded = client.post(f'/api/knowledge-bases/{base}/documents',
                               files={'files': ('private.txt', b'PRIVATE_ACCOUNT_DATA')}).json()[0]
        assert wait_document(client, base, uploaded['id'])['warning'] is None
        conversation = client.post('/api/conversations', json={}).json()['id']
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '查私有资料'})
        assert response.status_code == 200, response.text
        assert {key: results[-1][key] for key in ('query', 'results', 'status', 'detail')} == {
            'query': 'QUESTION_ONLY', 'results': [], 'status': 'no_knowledge_bases',
            'detail': '当前会话尚未绑定知识库，请先选择知识库。',
        }
        assert query_embeddings == []


def test_knowledge_citation_kind_distinguishes_text_table_and_image() -> None:
    """来源类型由真实切片结构确定，表格和图片不能继续按普通文字摘录展示。"""
    assert KnowledgeTools.source_kind({'structure': {'kind': 'paragraph'}}) == 'text'
    assert KnowledgeTools.source_kind({'structure': {'kind': 'table_row', 'table': 1}}) == 'table'
    assert KnowledgeTools.source_kind({'structure': {'kind': 'paragraph'}, 'image_id': 'image-1'}) == 'image'


def test_image_progress_is_observable_and_original_reaches_next_model_request(database_url: str, tmp_path: Path) -> None:
    """纯文字描述原样入库且进度可见，图片切片命中后下一轮实际携带原图。"""
    entered, release = Event(), Event()
    saw_original: list[bool] = []
    observed_messages: list[list[dict[str, Any]]] = []

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
            return tool_response('ORANGE_CHART')
        result = json.loads(tools[-1]['content'])
        assert result['results'][0]['image_id']
        assert result['results'][0]['content'] == 'ORANGE_CHART：橙色图表，季度增长柱状图。'
        parts = [part for item in payload['messages'] if isinstance(item.get('content'), list) for part in item['content']]
        observed_messages.append(payload['messages'])
        saw_original.append({'type': 'file', 'file_id': 'file-api-word-chart'} in parts)
        key = result['results'][0]['key']
        return httpx.Response(200, json={'choices': [{'message': {'content': f'图表说明季度增长。[{key}]'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '图表资料'}).json()['id']
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
        assert saw_original == [True], observed_messages
        source = response.json()['messages'][-1]['knowledge_sources'][0]
        assert source['image_id']
        assert source['kind'] == 'image'
        image = client.get(
            f'/api/knowledge-bases/{source["knowledge_base_id"]}/documents/{source["document_id"]}'
            f'/images/{source["image_id"]}/file'
        )
        assert image.status_code == 200
        assert image.headers['content-type'].startswith('image/')


def test_knowledge_image_caption_is_sent_with_image_and_excluded_from_text_chunks(
    database_url: str, tmp_path: Path,
) -> None:
    """知识库图注只随对应图片处理，并从普通文字切片中排除。"""
    prompts: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """响应 Files、视觉描述和知识库向量索引，记录视觉模型收到的完整输入。"""
        if request.url.path == '/files':
            return image_files_response()
        if request.method == 'DELETE':
            return httpx.Response(200)
        payload = json.loads(request.content)
        if 'input' in payload:
            return httpx.Response(200, json={'model': 'test-embedding', 'data': [
                {'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(payload['input'])]})
        parts = payload['messages'][0]['content']
        prompts.append(parts[0]['text'])
        assert any(part.get('type') == 'file' for part in parts)
        return httpx.Response(200, json={'choices': [{'message': {'content': '订单状态及风险冻结旁路图。'}}]})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='test-embedding', api_key='test-key', dimensions=2, revision='v1'))
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '图注测试'}).json()['id']
        uploaded = client.post(f'/api/knowledge-bases/{base}/documents',
                               files={'files': ('caption.docx', word_with_chart())}).json()[0]
        assert wait_document(client, base, uploaded['id'])['warning'] is None
    assert len(prompts) == 1
    assert '图 03 订单状态图及风险冻结旁路' in prompts[0]
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        chunks = connection.execute(
            'SELECT content,image_id,structure FROM knowledge_chunks WHERE document_id=%s ORDER BY ordinal',
            (uploaded['id'],),
        ).fetchall()
        images = connection.execute(
            'SELECT metadata FROM knowledge_document_images WHERE document_id=%s',
            (uploaded['id'],),
        ).fetchall()
    assert all('图 03 订单状态图及风险冻结旁路' not in row['content']
               for row in chunks if row['image_id'] is None)
    image_chunks = [row for row in chunks if row['image_id'] is not None]
    assert len(image_chunks) == 1
    assert image_chunks[0]['content'] == '订单状态及风险冻结旁路图。'
    assert image_chunks[0]['structure']['caption'] == '图 03 订单状态图及风险冻结旁路'
    assert images[0]['metadata']['caption'] == '图 03 订单状态图及风险冻结旁路'


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
