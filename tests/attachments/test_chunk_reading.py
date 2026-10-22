"""通过上传及文档工具验证阅读与检索共用切片。"""

from pathlib import Path
from uuid import UUID

import json
import pytest

from general_agent.attachments.service import AttachmentService
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.tokens import TokenCounter
from tests.attachments.test_attachments import configure, configure_settings
from tests.attachments.test_documents import pdf_bytes
from tests.attachments.test_knowledge import owner
from tests.auth.support import authenticated_client


def test_page_read_returns_the_entire_matching_search_chunk(database_url: str, tmp_path: Path) -> None:
    """请求中间页仍返回跨三页的完整检索切片，正文不裁剪或重新拆分。"""
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('pages.pdf', pdf_bytes(['BEFORE.', 'TARGET.', 'AFTER.']))}).json()[0]
        documents = AttachmentService(Database(database_url), tmp_path).documents
        account = owner(database_url, conversation)
        searched = documents.search(UUID(conversation), account, {'query': 'TARGET'})['results']
        assert len(searched) == 1
        assert 'BEFORE.' in searched[0]['content'] and 'AFTER.' in searched[0]['content']
        read = documents.read(UUID(conversation), account,
                              {'document_id': uploaded['id'], 'start_page': 2, 'end_page': 2},
                              TokenCounter('test'), 10000)
        assert read['complete'] and read['next_cursor'] is None
        assert [block['content'] for block in read['content']] == [searched[0]['content']]


def test_read_returns_all_chunks_in_source_order_with_whole_chunk_continuation(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按段落读取不限于搜索前五片，预算分页不拆片、不去重，续读恰好覆盖全部片。"""
    monkeypatch.setenv('DOCUMENT_CHUNK_TOKENS', '200')
    monkeypatch.setenv('DOCUMENT_CHUNK_OVERFLOW_TOKENS', '0')
    monkeypatch.setenv('DOCUMENT_SEARCH_RESULTS', '50')
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        text = '\n\n'.join(f'COMMON section {number:02d}. ' + 'detail ' * 20 for number in range(12))
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('sections.md', text.encode())}).json()[0]
        documents = AttachmentService(Database(database_url), tmp_path).documents
        account = owner(database_url, conversation)
        searched = documents.search(UUID(conversation), account, {'query': 'COMMON'})['results']
        expected = {chunk['chunk_id']: chunk['content'] for chunk in searched}
        assert len(expected) > 5
        counter = TokenCounter('test')
        request = {'document_id': uploaded['id'], 'start_paragraph': 1, 'end_paragraph': 12}
        collected = []
        cursors = []
        for _ in range(50):
            result = documents.read(UUID(conversation), account, request, counter, 800)
            assert 'error' not in result
            assert counter.count(json.dumps(result, ensure_ascii=False)) <= 800
            for block in result['content']:
                assert block['content'] == expected[block['chunk_id']]
                collected.append(block['chunk_id'])
            if result['complete']:
                assert result['next_cursor'] is None
                break
            cursor = result['next_cursor']
            assert cursor not in cursors
            cursors.append(cursor)
            request = {**request, 'cursor': cursor}
        else:
            pytest.fail('按整片续读未能完成。')
        assert collected == sorted(expected)
        assert len(cursors) > 1
        with pytest.raises(ValueError, match='续读位置'):
            documents.read(UUID(conversation), account, {**request, 'cursor': {'chunk_id': 9999}}, counter, 800)


def test_page_range_and_paragraph_range_return_empty_readable_pages(
    database_url: str, tmp_path: Path,
) -> None:
    """空白页可正常返回空结果，页码与段落参数不能混用，未知会话不得读取。"""
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        other = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('blank.pdf', pdf_bytes(['READABLE.', '']))}).json()[0]
        documents = AttachmentService(Database(database_url), tmp_path).documents
        account = owner(database_url, conversation)
        request = {'document_id': uploaded['id'], 'start_page': 2, 'end_page': 2}
        result = documents.read(UUID(conversation), account, request, TokenCounter('test'), 10000)
        assert result['content'] == [] and result['complete']
        with pytest.raises(ValueError, match='页码范围'):
            documents.read(UUID(conversation), account, {**request, 'start_paragraph': 1}, TokenCounter('test'), 10000)
        with pytest.raises(LookupError):
            documents.read(UUID(other), account, request, TokenCounter('test'), 10000)
