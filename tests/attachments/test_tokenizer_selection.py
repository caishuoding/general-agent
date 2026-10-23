"""通过应用文档处理配置验证 LLM 计数选择与模型切换。"""

import json
from pathlib import Path

import pytest
import psycopg
from psycopg import sql
import httpx
from tokenizers import Tokenizer, models, pre_tokenizers

from general_agent.attachments.knowledge import configured_chunk_policy
from general_agent.infrastructure.settings import EmbeddingSettingsStore, SettingsStore, StoredEmbeddingSettings, StoredSettings
from tests.auth.support import authenticated_client, login_admin, login_user
from tests.attachments.test_documents import pdf_bytes, image_pdf_bytes
from tests.attachments.test_document_images import image_files_response
from tests.knowledge.test_knowledge_base import wait_document


def test_new_document_policy_follows_llm_without_rewriting_existing_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """嵌入配置不影响切片计数；模型切换后新策略生效，既有任务快照保持不变。"""
    path = tmp_path / 'tokenizer.json'
    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'hello': 1, 'world': 2}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.save(str(path))
    monkeypatch.setenv('MODEL_TOKENIZERS', json.dumps({'llm-first': str(path)}))
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example', model='embedding', api_key='test', dimensions=1024))
    store = SettingsStore(tmp_path)
    store.write(StoredSettings(base_url='https://first.example', model='llm-first', api_key='test'))
    first = configured_chunk_policy(tmp_path)
    assert first.counter.model == 'llm-first'
    assert first.counter.count('hello world') == 2
    store.write(StoredSettings(base_url='https://second.example', model='llm-second', api_key='test'))
    second = configured_chunk_policy(tmp_path)
    assert second.counter.model == 'llm-second'
    assert second.counter.count('hello world') == 11
    assert first.counter.count('hello world') == 2
    assert first.signature != second.signature
    assert first.layout_signature == second.layout_signature


@pytest.mark.parametrize('kind', ['attachment', 'knowledge'])
@pytest.mark.parametrize('with_image', [False, True])
def test_live_llm_switch_applies_to_new_uploads_and_preserves_old_chunks(
    database_url: str, tmp_path: Path, kind: str, with_image: bool,
) -> None:
    """通过真实设置、上传和列表接口确认换 LLM 不会重切已有文字或含图文档。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """在外部模型边界模拟 Files 和纯文字图片描述，所有业务处理使用真实实现。"""
        if request.url.path == '/files':
            return image_files_response()
        return httpx.Response(200, json={'choices': [{'message': {'content': '橙色图表内容。'}}],
                                        'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25}})

    transport = httpx.MockTransport(provider)
    store = SettingsStore(tmp_path)
    store.write(StoredSettings(base_url='https://first.example', model='first-llm', api_key='test'))
    with authenticated_client(database_url, tmp_path, transport) as client:
        if kind == 'attachment':
            owner = client.post('/api/conversations', json={}).json()['id']
            url = f'/api/conversations/{owner}/attachments'
        else:
            owner = client.post('/api/knowledge-bases', json={'name': '模型切换'}).json()['id']
            url = f'/api/knowledge-bases/{owner}/documents'
        content = image_pdf_bytes() if with_image else pdf_bytes(['hello world'])
        response = client.post(url, files={'files': ('first.pdf', content)})
        assert response.status_code == 201, response.text
        identifier = response.json()[0]['id']
        first = (client.get(url).json()[0] if kind == 'attachment' else wait_document(client, owner, identifier))
        assert first['status'] == 'ready', first
        assert first['parse_info']['tokenizer_model'] == 'first-llm'
        fingerprint = first['parse_info']['tokenizer_fingerprint']
        client.post('/api/auth/logout')
        login_admin(client)
        changed = client.put('/api/settings', json={'base_url': 'https://second.example', 'model': 'second-llm', 'api_key': 'test'})
        assert changed.status_code == 200, changed.text
        client.post('/api/auth/logout')
        login_user(client)
        response = client.post(url, files={'files': ('second.pdf', content)})
        assert response.status_code == 201, response.text
        second_id = response.json()[0]['id']
        second = (next(row for row in client.get(url).json() if row['id'] == second_id) if kind == 'attachment'
                  else wait_document(client, owner, second_id))
        assert second['status'] == 'ready', second
        assert second['parse_info']['tokenizer_model'] == 'second-llm'
    with authenticated_client(database_url, tmp_path, transport) as client:
        first = next(row for row in client.get(url).json() if row['id'] == identifier)
        assert first['status'] == 'ready'
        assert first['parse_info']['tokenizer_fingerprint'] == fingerprint


@pytest.mark.parametrize('kind', ['attachment', 'knowledge'])
def test_restart_preserves_explicitly_cleared_document_indexes(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    """准备人工清空状态后，即使切片规则改变，启动也不能从原件恢复已清空内容。"""
    with authenticated_client(database_url, tmp_path) as client:
        if kind == 'attachment':
            owner = client.post('/api/conversations', json={}).json()['id']
            url, documents, chunks, key = f'/api/conversations/{owner}/attachments', 'attachments', 'document_chunks', 'attachment_id'
        else:
            owner = client.post('/api/knowledge-bases', json={'name': '人工清空'}).json()['id']
            url, documents, chunks, key = f'/api/knowledge-bases/{owner}/documents', 'knowledge_documents', 'knowledge_chunks', 'document_id'
        response = client.post(url, files={'files': ('clear.pdf', pdf_bytes(['hello world']))})
        assert response.status_code == 201, response.text
        identifier = response.json()[0]['id']
        if kind == 'knowledge':
            assert wait_document(client, owner, identifier)['status'] == 'ready'
    # 外部维护操作作为场景输入；验收只通过应用公开状态接口观察重启结果。
    with psycopg.connect(database_url) as connection:
        connection.execute(sql.SQL('DELETE FROM {} WHERE {}=%s').format(sql.Identifier(chunks), sql.Identifier(key)), (identifier,))
        connection.execute(sql.SQL("UPDATE {} SET parse_info=parse_info || '{{\"index_cleared\":true}}'::jsonb WHERE id=%s").format(sql.Identifier(documents)), (identifier,))
    monkeypatch.setenv('DOCUMENT_CHUNK_TOKENS', '1000')
    with authenticated_client(database_url, tmp_path) as client:
        document = client.get(url).json()[0]
        assert document['status'] == 'ready'
        assert document['parse_info']['index_cleared'] is True
