"""验证会话知识库的访问边界、连续读取预算、到期与派生数据清理。"""

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

from general_agent.attachments.service import AttachmentService
from general_agent.attachments.tools import DocumentTools, ReadDocumentInput, ReadCursor
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.settings import StoredSettings
from general_agent.infrastructure.tokens import TokenCounter
from general_agent.agent.graph import AgentToolGraph
from general_agent.memory.automatic import MemoryCheckpoint
from general_agent.infrastructure.provider import StreamChunk, ToolCallDelta
from tests.app.test_agent_graph import checkpoint, tool_chunks
from tests.auth.support import authenticated_client
from tests.attachments.test_attachments import configure, configure_settings, document_tool_reply, document_tool_result
from tests.attachments.test_documents import pdf_bytes, docx_bytes
from tests.attachments.test_images import image_bytes, upload_response


def service(database_url: str, directory: Path) -> AttachmentService:
    """构造与应用相同的附件服务，直接核验工具业务边界及持久化数据。"""
    return AttachmentService(Database(database_url), directory)


def owner(database_url: str, conversation: str) -> UUID:
    """从隔离测试数据库取得当前会话所属账号，不依赖固定用户身份。"""
    with Database(database_url).connect() as connection:
        row = connection.execute('SELECT user_id FROM conversations WHERE id=%s', (UUID(conversation),)).fetchone()
    assert row is not None
    return cast(UUID, row['user_id'])


def test_read_continuation_preserves_every_character_and_stays_in_budget() -> None:
    """用小预算反复跨段、跨页续读，所有原文字符恰好返回一次。"""
    counter = TokenCounter('no-official-tokenizer')
    text = '付款条件："人民币"\\\n\t' * 70
    units: list[dict[str, Any]] = [{'ordinal': 1, 'page': 1, 'paragraph': 1, 'content': text},
             {'ordinal': 2, 'page': 1, 'paragraph': 2, 'content': ''},
             {'ordinal': 3, 'page': 2, 'paragraph': 1, 'content': 'END\n' + text}]
    cursor = None
    collected: list[str] = []
    for _ in range(100):
        request = ReadDocumentInput(document_id=str(uuid4()), cursor=cursor)
        remaining = [unit for unit in units if cursor is None or unit['ordinal'] >= cursor.unit]
        result = DocumentTools.read_result(request, '报告.pdf', remaining, counter, 800)
        assert 'error' not in result
        assert counter.count(json.dumps(result, ensure_ascii=False)) <= 800
        collected.extend(block['content'] for block in result['content'])
        if result['complete']:
            break
        next_cursor = ReadCursor.model_validate(result['next_cursor'])
        assert next_cursor != cursor
        cursor = next_cursor
    else:
        raise AssertionError('连续读取未能完成。')
    assert ''.join(collected) == ''.join(str(unit['content']) for unit in units)


def test_read_reports_insufficient_context_without_a_stalled_cursor() -> None:
    """预算不够结果包装时返回明确错误，不能产生原地续读游标。"""
    result = DocumentTools.read_result(ReadDocumentInput(document_id=str(uuid4())), '长文.txt',
        [{'ordinal': 1, 'page': None, 'paragraph': 1, 'content': '中文原文'}], TokenCounter('unknown'), 10)
    assert result['code'] == 'context_budget_exhausted'
    assert 'next_cursor' not in result


def test_each_document_tool_has_five_calls_including_failures_and_batches() -> None:
    """两个工具分别最多五次；同批第六次被拦截，失败也占次数，新回答重置。"""
    calls = MagicMock()
    calls.stream.side_effect = [tool_chunks(['search_documents'] * 7 + ['read_document'] * 6, 'batch'),
                               [StreamChunk(content='已有内容。')]]
    execute = MagicMock(return_value={'error': '可恢复错误'})
    tools = [{'type': 'function', 'function': {'name': name}} for name in ('search_documents', 'read_document')]
    arguments: dict[str, Any] = dict(messages=[{'role': 'user', 'content': '读取'}],
        settings=StoredSettings(base_url='https://example.com', model='test', api_key='test'), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()), tools=tools, execute_tool=execute)
    graph = AgentToolGraph()
    result = graph.run(**arguments)
    assert execute.call_count == 10
    assert result['document_calls'] == {'search_documents': 5, 'read_document': 5}
    assert calls.stream.call_args.kwargs['tools'] is None
    calls.stream.side_effect = [tool_chunks(['read_document'], 'new'), [StreamChunk(content='新回答。')]]
    arguments['checkpoint'] = cast(MemoryCheckpoint, checkpoint())
    assert graph.run(**arguments)['document_calls'] == {'read_document': 1}
    assert execute.call_count == 11


def test_invalid_document_json_consumes_call_limit() -> None:
    """畸形 JSON 也计入文档调用次数，第六次不得进入业务执行器。"""
    calls = MagicMock()
    calls.stream.side_effect = [[StreamChunk(tool_call=ToolCallDelta(index=index, id=str(index),
        name='read_document', arguments='invalid json')) for index in range(6)], [StreamChunk(content='参数无效。')]]
    execute = MagicMock()
    result = AgentToolGraph().run(messages=[{'role': 'user', 'content': '读取'}],
        settings=StoredSettings(base_url='https://example.com', model='test', api_key='test'), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()),
        tools=[{'type': 'function', 'function': {'name': 'read_document'}}], execute_tool=execute)
    assert result['document_calls'] == {'read_document': 5}
    execute.assert_not_called()
    assert calls.stream.call_args.kwargs['tools'] is None


def test_document_search_is_complete_and_scoped(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """检索只按片数返回完整切片，显式伪造其他会话或账号身份均拒绝。"""
    monkeypatch.setenv('DOCUMENT_CHUNK_CHARACTERS', '200')
    monkeypatch.setenv('DOCUMENT_SEARCH_RESULTS', '2')
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        first, second = configure(client), configure(client)
        uploaded = client.post(f'/api/conversations/{first}/attachments', files={'files': ('计划.txt', ('交付延期。' * 500).encode())}).json()[0]
        attachments = service(database_url, tmp_path)
        account = owner(database_url, first)
        result = attachments.documents.search(UUID(first), account, {'query': '交付延期'})
        assert len(result['results']) == 2
        assert all(len(chunk['content']) == 200 for chunk in result['results'])
        assert attachments.documents.search(UUID(second), account, {'query': '交付延期'})['results'] == []
        with pytest.raises(LookupError):
            attachments.documents.search(UUID(second), account, {'query': '交付', 'document_ids': [uploaded['id']]})
        with pytest.raises(LookupError):
            attachments.documents.read(UUID(first), uuid4(), {'document_id': uploaded['id']}, TokenCounter('test'), 10000)


def test_too_many_short_paragraphs_fail_before_indexing(database_url: str, tmp_path: Path) -> None:
    """大量短行不能绕过解析资源限制，失败文档不进入清单或生成部分索引。"""
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('many.txt', b'x\n' * 20001)}).json()[0]
        listed = client.get(f'/api/conversations/{conversation}/attachments').json()[0]
        assert listed['status'] == 'failed' and '资源限制' in listed['error']
        with Database(database_url).connect() as connection:
            assert connection.execute('SELECT * FROM document_units WHERE attachment_id=%s', (UUID(uploaded['id']),)).fetchall() == []


def test_pdf_reads_original_pages_and_honors_current_budget(database_url: str, tmp_path: Path) -> None:
    """长页原文可跨切片连续读，按范围核验游标，并执行小于万 Token 的实际预算。"""
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        text = 'ORIGINALPAGE ' * 1000
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('long.pdf', pdf_bytes([text, 'PAGE_TWO']))}).json()[0]
        attachments = service(database_url, tmp_path)
        counter = TokenCounter('test')
        account = owner(database_url, conversation)
        request = {'document_id': uploaded['id'], 'start_page': 1, 'end_page': 1}
        first = attachments.documents.read(UUID(conversation), account, request, counter, 1500)
        assert not first['complete']
        assert counter.count(json.dumps(first, ensure_ascii=False)) <= 1500
        assert first['content'][0]['content'] == text[:len(first['content'][0]['content'])]
        next_result = attachments.documents.read(UUID(conversation), account, {**request, 'cursor': first['next_cursor']}, counter, 20000)
        assert next_result['content'][0]['start_offset'] == first['content'][0]['end_offset']
        assert counter.count(json.dumps(next_result, ensure_ascii=False)) <= 10000
        with pytest.raises(ValueError):
            attachments.documents.read(UUID(conversation), account, {**request, 'start_page': 2, 'end_page': 2, 'cursor': first['next_cursor']}, counter, 10000)
        second = attachments.documents.read(UUID(conversation), account, {'document_id': uploaded['id'], 'start_page': 2, 'end_page': 2}, counter, 10000)
        assert second['content'][0]['content'] == 'PAGE_TWO'


def test_expiration_immediately_hides_and_cleans_knowledge(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """到期先拒绝清单、下载和工具；清理失败后重试删除原文、切片及 Word 副本。"""
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('计划.docx', docx_bytes())}).json()[0]
        identifier = UUID(uploaded['id'])
        attachments = service(database_url, tmp_path)
        account = owner(database_url, conversation)
        with attachments.database.connect() as connection:
            row = connection.execute('SELECT expires_at-created_at AS lifetime FROM attachments WHERE id=%s', (identifier,)).fetchone()
            assert row is not None and row['lifetime'].total_seconds() == 30 * 86400
            connection.execute("UPDATE attachments SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE id=%s", (identifier,))
        assert client.get(f'/api/conversations/{conversation}/attachments').json() == []
        assert client.get(f'/api/conversations/{conversation}/attachments/{identifier}/file').status_code == 404
        assert attachments.prepare(UUID(conversation), account).messages == []
        assert attachments.documents.search(UUID(conversation), account, {'query': 'ORBIT'})['results'] == []
        with pytest.raises(LookupError):
            attachments.documents.read(UUID(conversation), account, {'document_id': str(identifier)}, TokenCounter('test'), 10000)
        original_delete = attachments.delete_file
        monkeypatch.setattr(attachments, 'delete_file', MagicMock(side_effect=OSError('暂时不可清理')))
        attachments.expire()
        assert (tmp_path / 'attachments' / str(identifier)).exists()
        monkeypatch.setattr(attachments, 'delete_file', original_delete)
        attachments.expire()
        assert not (tmp_path / 'attachments' / str(identifier)).exists()
        assert not (tmp_path / 'attachments' / f'{identifier}.pdf').exists()
        with attachments.database.connect() as connection:
            assert connection.execute('SELECT content,expired_cleaned FROM attachments WHERE id=%s', (identifier,)).fetchone() == {'content': None, 'expired_cleaned': True}
            assert connection.execute('SELECT * FROM document_units WHERE attachment_id=%s', (identifier,)).fetchall() == []
            assert connection.execute('SELECT * FROM document_chunks WHERE attachment_id=%s', (identifier,)).fetchall() == []


def test_legacy_attachment_rebuild_keeps_original_ttl(database_url: str, tmp_path: Path) -> None:
    """已有文档从原件重建后可检索，升级和重启不会刷新附件有效期。"""
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('legacy.txt', b'LEGACY searchable original')}).json()[0]
        attachments = service(database_url, tmp_path)
        with attachments.database.connect() as connection:
            before = connection.execute('UPDATE attachments SET knowledge_version=0 WHERE id=%s RETURNING expires_at', (UUID(uploaded['id']),)).fetchone()
            connection.execute('DELETE FROM document_units WHERE attachment_id=%s', (UUID(uploaded['id']),))
        attachments.recover()
        assert attachments.documents.search(UUID(conversation), owner(database_url, conversation), {'query': 'LEGACY'})['results']
        with attachments.database.connect() as connection:
            assert connection.execute('SELECT expires_at FROM attachments WHERE id=%s', (UUID(uploaded['id']),)).fetchone() == before


def test_real_prompt_usage_constrains_reading_budget(database_url: str, tmp_path: Path) -> None:
    """即使本地清单很短，实际模型用量耗尽窗口时原文工具也不能返回正文。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """先请求原文并报告窗口已满，再确认工具返回预算不足而不是正文。"""
        payload = json.loads(request.content)
        reply = document_tool_reply(payload)
        if reply is not None:
            response = reply.json()
            response['usage'] = {'prompt_tokens': 31000, 'completion_tokens': 30, 'total_tokens': 31030}
            return httpx.Response(200, json=response)
        result = document_tool_result(payload)
        assert result['code'] == 'context_budget_exhausted'
        assert 'PRIVATE_ORIGINAL' not in str(result)
        return httpx.Response(200, json={'choices': [{'message': {'content': '当前上下文空间不足。'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('secret.txt', b'PRIVATE_ORIGINAL')})
        response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '读取文件'})
        assert response.json()['messages'][-1]['content'] == '当前上下文空间不足。'


def test_image_app_ttl_cleans_remote_file_and_preserves_chat(database_url: str, tmp_path: Path) -> None:
    """应用层到期禁止图片引用并清理远端及本地文件，已经完成的聊天正文保留。"""
    deleted: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """提供真实 Files 协议替身并记录到期清理调用，不使用私人图片。"""
        if request.method == 'DELETE':
            deleted.append(request.url.path)
            return httpx.Response(200, json={'id': 'file-api-expiring', 'object': 'file', 'deleted': True})
        if request.url.path == '/files':
            return upload_response(request, 'file-api-expiring')
        return httpx.Response(200, json={'choices': [{'message': {'content': '已保存的回答。'}}]})

    configure_settings(database_url, tmp_path)
    transport = httpx.MockTransport(provider)
    with authenticated_client(database_url, tmp_path, transport) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('image.png', image_bytes())}).json()[0]
        assert client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status'] == 'ready'
        client.post(f'/api/conversations/{conversation}/messages', json={'content': '看图'})
        attachments = AttachmentService(Database(database_url), tmp_path, transport)
        with attachments.database.connect() as connection:
            connection.execute("UPDATE attachments SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE id=%s", (UUID(uploaded['id']),))
        with pytest.raises(ValueError):
            attachments.image_file(UUID(uploaded['id']))
        attachments.expire()
        assert deleted == ['/files/file-api-expiring']
        assert not (tmp_path / 'attachments' / uploaded['id']).exists()
        assert client.get(f'/api/conversations/{conversation}').json()['messages'][-1]['content'] == '已保存的回答。'
