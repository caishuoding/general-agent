"""从知识库公开接口验证独立存储、会话选择和长期资料的生命周期。"""

from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

from tests.auth.support import authenticated_client, ensure_normal_user, login_user
from tests.attachments.test_documents import docx_bytes, pdf_bytes


def wait_document(client: TestClient, base: str, identifier: str) -> dict[str, Any]:
    """通过真实状态接口等待后台文档终态，不检查任务内部实现。"""
    for _ in range(200):
        rows = client.get(f'/api/knowledge-bases/{base}/documents').json()
        document: dict[str, Any] = next(row for row in rows if row['id'] == identifier)
        if document['status'] in ('ready', 'failed'):
            return document
        time.sleep(0.05)
    raise AssertionError('知识库文档处理超时')


def test_knowledge_lives_across_conversations_and_enforces_account_scope(database_url: str, tmp_path: Path) -> None:
    """独立资料可被多个会话选择，其他账号不能读取，删除会话也不删除资料。"""
    with authenticated_client(database_url, tmp_path) as client:
        response = client.post('/api/knowledge-bases', json={'name': '产品资料'})
        assert response.status_code == 201, response.text
        base = response.json()['id']
        first = client.post('/api/conversations', json={}).json()['id']
        second = client.post('/api/conversations', json={}).json()['id']
        for conversation in (first, second):
            selected = client.put(f'/api/conversations/{conversation}/knowledge-bases', json={'knowledge_base_ids': [base]})
            assert selected.status_code == 200, selected.text
            assert [row['id'] for row in selected.json()] == [base]
        uploaded = client.post(f'/api/knowledge-bases/{base}/documents', files={'files': ('manual.txt', b'alpha product handbook')})
        assert uploaded.status_code == 201, uploaded.text
        document = wait_document(client, base, uploaded.json()[0]['id'])
        assert document['status'] == 'ready', document
        assert document['stage'] == 'complete'
        assert document['warning'], '未配置向量时必须明确展示降级'
        assert client.get(f'/api/conversations/{first}/attachments').json() == []
        assert client.get(f'/api/knowledge-bases/{base}/documents/{document["id"]}/file').content == b'alpha product handbook'
        assert client.put(f'/api/conversations/{second}/knowledge-bases', json={'knowledge_base_ids': []}).json() == []
        ensure_normal_user(database_url, 'another_user')
        login_user(client, 'another_user')
        assert client.get('/api/knowledge-bases').json() == []
        assert client.get(f'/api/knowledge-bases/{base}/documents').status_code == 404
        own = client.post('/api/conversations', json={}).json()['id']
        assert client.put(f'/api/conversations/{own}/knowledge-bases', json={'knowledge_base_ids': [base]}).status_code == 404
        login_user(client)
        preview = client.get(f'/api/conversations/{first}/deletion-preview').json()
        assert client.request('DELETE', f'/api/conversations/{first}', json={'preview_revision': preview['revision']}).status_code == 200
        assert len(client.get(f'/api/knowledge-bases/{base}/documents').json()) == 1
        assert client.delete(f'/api/knowledge-bases/{base}').status_code == 200
        for _ in range(100):
            if not client.get('/api/knowledge-bases').json():
                break
            time.sleep(0.02)
        assert client.get('/api/knowledge-bases').json() == []


@pytest.mark.parametrize('extension', ['docx', 'pdf', 'md'])
def test_supported_document_formats_keep_real_positions(database_url: str, tmp_path: Path, extension: str) -> None:
    """Word、PDF 和 Markdown 使用真实解析链路，并保存适合各格式的位置。"""
    content = docx_bytes() if extension == 'docx' else pdf_bytes(['alpha handbook']) if extension == 'pdf' else b'# Alpha\n\nhandbook'
    with authenticated_client(database_url, tmp_path) as client:
        base = client.post('/api/knowledge-bases', json={'name': '格式验证'}).json()['id']
        uploaded = client.post(f'/api/knowledge-bases/{base}/documents', files={'files': (f'manual.{extension}', content)})
        assert uploaded.status_code == 201, uploaded.text
        document = wait_document(client, base, uploaded.json()[0]['id'])
        assert document['status'] == 'ready', document
        assert document['parse_info']['format'] == extension
        assert document['parse_info']['total_units'] >= 1
        if extension == 'docx':
            assert document['parse_info']['text_source'] == 'word_native_markdown_v3'
        elif extension == 'pdf':
            assert document['parse_info']['text_source'] == 'pdf_tables_v2'
