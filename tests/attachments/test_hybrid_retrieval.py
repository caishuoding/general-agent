"""通过文档工具和外部 HTTP 边界验证混合召回，不使用真实服务凭据。"""

import json

import httpx
import pytest

from pathlib import Path
from uuid import UUID

from general_agent.attachments.service import AttachmentService
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.settings import EmbeddingSettingsStore, StoredEmbeddingSettings
from tests.attachments.test_attachments import configure, configure_settings
from tests.attachments.test_knowledge import owner
from tests.auth.support import authenticated_client


def test_bm25_fallback_returns_complete_scoped_chunks(database_url: str, tmp_path: Path) -> None:
    """嵌入和重排未配置时仍返回 BM25 命中，并明确声明降级和候选数量。"""
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        other = configure(client)
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('plan.txt', b'alpha delivery plan')})
        client.post(f'/api/conversations/{other}/attachments', files={'files': ('private.txt', b'alpha SECRET')})
        documents = AttachmentService(Database(database_url), tmp_path).documents
        result = documents.search(UUID(conversation), owner(database_url, conversation), {'query': 'alpha'})
        assert [item['content'] for item in result['results']] == ['alpha delivery plan']
        assert result['retrieval']['mode'] == 'bm25'
        assert result['retrieval']['bm25_candidates'] == 1
        assert result['retrieval']['vector_candidates'] == 0
        assert result['retrieval']['reranked'] is False
        assert 'embedding_unavailable' in result['retrieval']['degraded']
        assert documents.search(UUID(conversation), owner(database_url, conversation), {'query': 'unrelated'})['results'] == []


def test_semantic_recall_reuses_embedding_and_zhipu_reranks(database_url: str, tmp_path: Path) -> None:
    """无关键词交集的切片仍被语义召回，重排收到去重候选且重复查询复用向量。"""
    inputs: list[list[str]] = []
    candidates: list[list[str]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """用受控向量表示同义词，验证实际嵌入配置与智谱 HTTP 合同。"""
        payload = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            assert request.headers['Authorization'] == 'Bearer embedding-test-key'
            assert payload['model'] == 'existing-embedding'
            assert payload['dimensions'] == 2
            inputs.append(payload['input'])
            vectors = [{'index': index, 'embedding': [1.0, 0.0] if 'vehicle' in text or 'car' in text else [0.0, 1.0]}
                       for index, text in enumerate(payload['input'])]
            return httpx.Response(200, json={'model': 'existing-embedding', 'data': list(reversed(vectors))})
        assert str(request.url) == 'https://open.bigmodel.cn/api/paas/v4/rerank'
        assert request.headers['Authorization'] == 'Bearer rerank-test-key'
        assert payload['model'] == 'rerank'
        candidates.append(payload['documents'])
        ordered = sorted(range(len(payload['documents'])), key=payload['documents'].__getitem__, reverse=True)
        return httpx.Response(200, json={'results': [{'index': index, 'relevance_score': 1.0 - rank / 10}
                                                    for rank, index in enumerate(ordered)]})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='existing-embedding', api_key='embedding-test-key', dimensions=2, revision='space-1'))
    (tmp_path / 'rerank.json').write_text(json.dumps({'api_key': 'rerank-test-key'}))
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('transport.txt', b'vehicle maintenance')})
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('food.txt', b'apple recipe')})
        documents = AttachmentService(Database(database_url), tmp_path, httpx.MockTransport(provider)).documents
        account = owner(database_url, conversation)
        first = documents.search(UUID(conversation), account, {'query': 'car'})
        assert first['results'][0]['content'] == 'vehicle maintenance'
        assert first['retrieval']['bm25_candidates'] == 0
        assert first['retrieval']['vector_candidates'] == 2
        assert first['retrieval']['reranked'] is True
        assert first['retrieval']['degraded'] == []
        assert len(candidates[0]) == len(set(candidates[0])) == 2
        documents.search(UUID(conversation), account, {'query': 'car'})
        assert sum('vehicle maintenance' in batch for batch in inputs) == 1
        assert inputs.count(['car']) == 2


def test_deleted_source_is_not_embedded_after_query_request(database_url: str, tmp_path: Path) -> None:
    """查询向量生成期间删除来源，后续不能再发送其正文、返回内容或恢复缓存。"""
    requests: list[list[str]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """在查询向量返回前通过正式删除接口撤回来源。"""
        payload = json.loads(request.content)
        requests.append(payload['input'])
        assert payload['input'] == ['car'], '删除来源的正文不应继续发送给嵌入服务'
        path = f'/api/conversations/{conversation}/attachments/{uploaded["id"]}'
        preview = client.get(path + '/deletion-preview').json()
        assert client.request('DELETE', path, json={'preview_revision': preview['revision']}).status_code == 200
        return httpx.Response(200, json={'model': 'existing-embedding', 'data': [{'index': 0, 'embedding': [1.0, 0.0]}]})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='existing-embedding', api_key='test', dimensions=2, revision='space-1'))
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('secret.txt', b'vehicle PRIVATE')}).json()[0]
        documents = AttachmentService(Database(database_url), tmp_path, httpx.MockTransport(provider)).documents
        result = documents.search(UUID(conversation), owner(database_url, conversation), {'query': 'car'})
        assert result['results'] == []
        assert requests == [['car']]


def test_rrf_bounds_candidates_and_rerank_controls_final_order(database_url: str, tmp_path: Path) -> None:
    """两路各三十片按 RRF 去重，交集靠前，重排三十片后只返回五片完整原文。"""
    sent: list[list[str]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """构造两路不同排序，并让重排显式反转融合顺序以证明模型结果被采用。"""
        payload = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            vectors = []
            for index, text in enumerate(payload['input']):
                high = text == 'alpha' or int(text.split()[2]) >= 10
                vectors.append({'index': index, 'embedding': [1.0, 0.0] if high else [0.0, 1.0]})
            return httpx.Response(200, json={'model': 'existing-embedding', 'data': vectors})
        sent.append(payload['documents'])
        assert payload['top_n'] == 30
        return httpx.Response(200, json={'results': [{'index': index, 'relevance_score': index / 30}
                                                    for index in range(30)]})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='existing-embedding', api_key='test', dimensions=2, revision='space-1'))
    (tmp_path / 'rerank.json').write_text(json.dumps({'api_key': 'test'}))
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        text = ''.join(f'# Section {index:02d}\nalpha details.\n\n' for index in range(40))
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('many.md', text.encode())})
        documents = AttachmentService(Database(database_url), tmp_path, httpx.MockTransport(provider)).documents
        result = documents.search(UUID(conversation), owner(database_url, conversation), {'query': 'alpha'})
        assert result['retrieval']['bm25_candidates'] == 30
        assert result['retrieval']['vector_candidates'] == 30
        assert result['retrieval']['fused_candidates'] == 30
        assert sent[0][0].startswith('# Section 10\n')
        assert len(sent[0]) == len(set(sent[0])) == 30
        assert [row['content'] for row in result['results']] == list(reversed(sent[0][-5:]))
        assert all('embedding' not in row and 'embedding_revision' not in row for row in result['results'])


@pytest.mark.parametrize('failure', ['timeout', 'http', 'duplicate', 'out_of_bounds', 'nan', 'missing', 'embedding'])
def test_failures_fall_back_without_exposing_credentials(database_url: str, tmp_path: Path, failure: str) -> None:
    """网络失败和畸形重排结果均明确降级；嵌入失败保持 BM25 可用，不暴露密钥。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """在真实 HTTP 边界注入各类故障，不替换应用内部召回组件。"""
        payload = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            if failure == 'embedding':
                return httpx.Response(200, json={'model': 'wrong-space', 'data': [{'index': 0, 'embedding': [1.0]}]})
            vectors = [{'index': index, 'embedding': [0.0, 1.0] if text == 'car manual' else [1.0, 0.0]}
                       for index, text in enumerate(payload['input'])]
            return httpx.Response(200, json={'model': 'existing-embedding', 'data': vectors})
        if failure == 'timeout':
            raise httpx.ReadTimeout('SECRET_FROM_PROVIDER', request=request)
        if failure in ('http', 'embedding'):
            return httpx.Response(503, text='SECRET_FROM_PROVIDER')
        if failure == 'missing':
            return httpx.Response(200, json={})
        indices = [0, 0] if failure == 'duplicate' else [0, 99] if failure == 'out_of_bounds' else [0, 1]
        results = [{'index': index, 'relevance_score': 0.5} for index in indices]
        if failure == 'nan':
            return httpx.Response(200, text='{"results":[{"index":0,"relevance_score":NaN},{"index":1,"relevance_score":0.5}]}')
        return httpx.Response(200, json={'results': results})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='existing-embedding', api_key='embedding-secret', dimensions=2, revision='space-1'))
    (tmp_path / 'rerank.json').write_text(json.dumps({'api_key': 'rerank-secret'}))
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        for text in ('car manual', 'vehicle guide'):
            client.post(f'/api/conversations/{conversation}/attachments', files={'files': (text + '.txt', text.encode())})
        documents = AttachmentService(Database(database_url), tmp_path, httpx.MockTransport(provider)).documents
        result = documents.search(UUID(conversation), owner(database_url, conversation), {'query': 'car'})
        expected = ['car manual'] if failure == 'embedding' else ['car manual', 'vehicle guide']
        assert [row['content'] for row in result['results']] == expected
        assert result['retrieval']['reranked'] is False
        assert 'rerank_unavailable' in result['retrieval']['degraded']
        assert result['retrieval']['mode'] == ('bm25' if failure == 'embedding' else 'hybrid')
        assert 'secret' not in json.dumps(result).lower()
        assert 'SECRET_FROM_PROVIDER' not in json.dumps(result)


def test_embedding_configuration_change_rebuilds_cached_vectors(database_url: str, tmp_path: Path) -> None:
    """重启复用持久向量，配置版本切换后只在新空间重建，结果不混用旧向量。"""
    inputs: list[tuple[str, list[str]]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """为两个向量空间提供不同维数，确保旧缓存无法冒充新模型结果。"""
        payload = json.loads(request.content)
        inputs.append((payload['model'], payload['input']))
        vector = [1.0, 0.0] if payload['model'] == 'first' else [0.0, 0.0, 1.0]
        return httpx.Response(200, json={'model': payload['model'], 'data': [
            {'index': index, 'embedding': vector} for index in range(len(payload['input']))]})

    configure_settings(database_url, tmp_path)
    store = EmbeddingSettingsStore(tmp_path)
    store.write(StoredEmbeddingSettings(base_url='https://embedding.example/v1', model='first', api_key='test', dimensions=2, revision='space-1'))
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('guide.txt', b'vehicle guide')})
        account = owner(database_url, conversation)
        for _ in range(2):
            documents = AttachmentService(Database(database_url), tmp_path, httpx.MockTransport(provider)).documents
            assert documents.search(UUID(conversation), account, {'query': 'car'})['results'][0]['content'] == 'vehicle guide'
        assert inputs.count(('first', ['vehicle guide'])) == 1
        store.write(StoredEmbeddingSettings(base_url='https://embedding.example/v1', model='second', api_key='test', dimensions=3, revision='space-2'))
        result = documents.search(UUID(conversation), account, {'query': 'car'})
        assert result['retrieval']['mode'] == 'hybrid'
        assert inputs.count(('second', ['vehicle guide'])) == 1
        assert result['results'][0]['content'] == 'vehicle guide'


def test_large_result_configuration_remains_effective(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """既有五十片配置同步提高候选上限，不被默认三十片截断。"""
    monkeypatch.setenv('DOCUMENT_SEARCH_RESULTS', '50')
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        text = ''.join(f'# Section {index:02d}\nalpha details.\n\n' for index in range(60))
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('many.md', text.encode())})
        result = AttachmentService(Database(database_url), tmp_path).documents.search(
            UUID(conversation), owner(database_url, conversation), {'query': 'alpha'})
        assert result['limit'] == 50
        assert len(result['results']) == result['retrieval']['bm25_candidates'] == 50
        assert result['retrieval']['fused_candidates'] == 50


def test_oversized_rerank_input_falls_back_without_truncation(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式配置一万 Token 后，大切片超过智谱限制时不调用重排，正文保持完整并说明原因。"""
    monkeypatch.setenv('DOCUMENT_CHUNK_TOKENS', '10000')
    configure_settings(database_url, tmp_path)
    (tmp_path / 'rerank.json').write_text(json.dumps({'api_key': 'test'}))

    def provider(request: httpx.Request) -> httpx.Response:
        """任何 HTTP 调用均说明超长输入未被提前拦截。"""
        raise AssertionError('超长切片不应发送给重排服务')

    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        text = 'alpha ' + 'details ' * 700
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('long.txt', text.encode())})
        result = AttachmentService(Database(database_url), tmp_path, httpx.MockTransport(provider)).documents.search(
            UUID(conversation), owner(database_url, conversation), {'query': 'alpha'})
        assert result['results'][0]['content'] == text
        assert 'rerank_input_too_long' in result['retrieval']['degraded']
        assert result['retrieval']['reranked'] is False
