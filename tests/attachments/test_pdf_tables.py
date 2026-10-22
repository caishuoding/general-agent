"""验证文字型 PDF 的表格识别、正文顺序、数字和共用切片。"""

from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, StreamObject

from general_agent.attachments.documents import parse_pdf
from general_agent.attachments.knowledge import ChunkPolicy, chunk_specs


def positioned_pdf(commands: list[str]) -> bytes:
    """生成带合法间接内容流的定位 PDF，用独立绘制指令验证真实解析而非模拟表格结果。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=500, height=900)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data('\n'.join(commands).encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def table_pdf(*, ruled: bool = True, rows: int = 8, merged: bool = False) -> bytes:
    """构造正文夹表格的真实 PDF，覆盖有框、无框和横向合并。"""
    commands = ['BT /F1 11 Tf 30 860 Td (BEFORE TABLE) Tj ET']
    top, height = 820, min(21, 700 / (rows + 1))
    if ruled:
        for row in range(rows + 2):
            y = top - row * height
            commands.append(f'30 {y} m 470 {y} l S')
        for x in (30, 190, 330, 470):
            if merged and x == 190:
                commands.append(f'{x} {top-height} m {x} {top-(rows+1)*height} l S')
            else:
                commands.append(f'{x} {top} m {x} {top-(rows+1)*height} l S')
    values = [['Name', 'Budget', 'CPC'], *[[f'ITEM{index:02d}', f'{400+index}.00', '5.42'] for index in range(rows)]]
    if merged:
        values[0] = ['Name and Budget', '', 'CPC']
        values[1][0] = 'A|B'
    for row, cells in enumerate(values):
        for x, cell in zip((35, 195, 335), cells):
            commands.append(f'BT /F1 10 Tf {x} {top-row*height-14} Td ({cell}) Tj ET')
    commands.append(f'BT /F1 11 Tf 30 {top-(rows+1)*height-35} Td (AFTER TABLE) Tj ET')
    return positioned_pdf(commands)


@pytest.mark.parametrize('ruled', [True, False])
def test_text_pdf_tables_keep_rows_digits_and_body_order(tmp_path: Path, ruled: bool) -> None:
    """行列识别必须覆盖有框和无框表格，正文不重复表格内容且顺序保持正确。"""
    path = tmp_path / 'table.pdf'
    path.write_bytes(table_pdf(ruled=ruled))
    parsed = parse_pdf(path)
    assert parsed.error is None
    rows = [unit for unit in parsed.units if unit.kind == 'table_row']
    assert len(rows) == 9
    assert rows[0].content == '| Name | Budget | CPC |\n| --- | --- | --- |\n'
    assert rows[1].content == '| ITEM00 | 400.00 | 5.42 |\n'
    text = ''.join(unit.content for unit in parsed.units)
    assert text.index('BEFORE TABLE') < text.index('| Name |') < text.index('AFTER TABLE')
    assert text.count('ITEM00') == 1
    assert all(unit.page == 1 for unit in parsed.units)


def test_long_pdf_table_chunks_repeat_header_within_token_budget(tmp_path: Path) -> None:
    """长 PDF 表格按数据行拆片，每片重复表头，正文独立切片且数据行不重复。"""
    path = tmp_path / 'long.pdf'
    path.write_bytes(table_pdf(rows=48))
    parsed = parse_pdf(path)
    assert parsed.error is None
    policy = ChunkPolicy()
    text = ''.join(unit.content for unit in parsed.units)
    contents = [spec.prefix + text[spec.start:spec.end] + spec.suffix for spec in chunk_specs(parsed.units, policy)]
    tables = [content for content in contents if content.startswith('| Name |')]
    assert len(tables) > 1
    assert all(content.startswith('| Name | Budget | CPC |\n| --- | --- | --- |\n') for content in tables)
    assert all(policy.counter.count(content) <= 1000 for content in contents)
    assert all('BEFORE TABLE' not in content and 'AFTER TABLE' not in content for content in tables)
    assert sum(content.count('ITEM00') for content in tables) == 1
    assert sum(content.count('ITEM47') for content in tables) == 1


def test_merged_pdf_header_preserves_empty_column_and_escapes_pipe(tmp_path: Path) -> None:
    """横向合并表头保留列位置，数据中的管道字符不能破坏 Markdown 结构。"""
    path = tmp_path / 'merged.pdf'
    path.write_bytes(table_pdf(merged=True))
    parsed = parse_pdf(path)
    assert parsed.error is None
    rows = [unit for unit in parsed.units if unit.kind == 'table_row']
    assert rows[0].content.startswith('| Name and Budget |  | CPC |')
    assert 'A\\|B' in rows[1].content


def test_aligned_prose_is_not_misidentified_as_borderless_table(tmp_path: Path) -> None:
    """普通单栏与双栏长段落不应仅因文字对齐就被误判成表格。"""
    commands = []
    for row in range(5):
        for x in (30, 270):
            commands.append(f'BT /F1 9 Tf {x} {820-row*15} Td (This is a paragraph with many words about the project.) Tj ET')
    path = tmp_path / 'prose.pdf'
    path.write_bytes(positioned_pdf(commands))
    parsed = parse_pdf(path)
    assert parsed.error is None
    assert not any(unit.table is not None for unit in parsed.units)
    assert ''.join(unit.content for unit in parsed.units).count('This is a paragraph') == 10


def test_multiline_cell_stays_in_one_markdown_row(tmp_path: Path) -> None:
    """同一网格单元格内的两行文字应保留为 br 换行，而非拆成两条数据行。"""
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(table_pdf()))
    writer = PdfWriter()
    writer.append(reader)
    stream = writer.pages[0]['/Contents'].get_object()
    assert isinstance(stream, StreamObject)
    commands = stream.get_data().decode('ascii').replace('35 785 Td (ITEM00)', '35 788 Td (ITEM00)')
    commands += '\nBT /F1 7 Tf 35 778 Td (DETAIL) Tj ET'
    stream.set_data(commands.encode('ascii'))
    output = BytesIO()
    writer.write(output)
    path = tmp_path / 'multiline.pdf'
    path.write_bytes(output.getvalue())
    parsed = parse_pdf(path)
    assert parsed.error is None
    rows = [unit for unit in parsed.units if unit.kind == 'table_row']
    assert len(rows) == 9
    assert 'ITEM00<br>DETAIL' in rows[1].content


def test_pdf_upload_search_read_and_legacy_recovery_share_table_chunks(database_url: str, tmp_path: Path) -> None:
    """真实上传后检索和阅读共用 Markdown 片；旧 PDF 启动升级保持原 TTL，匹配版本不再重建。"""
    from uuid import UUID
    from psycopg.types.json import Jsonb
    from general_agent.attachments.service import AttachmentService
    from general_agent.attachments.tools import DocumentTools
    from general_agent.infrastructure.database import Database
    from general_agent.infrastructure.tokens import TokenCounter
    from tests.attachments.test_attachments import configure, configure_settings
    from tests.attachments.test_knowledge import owner
    from tests.auth.support import authenticated_client

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('table.pdf', table_pdf(rows=48))}).json()[0]
        listed = client.get(f'/api/conversations/{conversation}/attachments').json()[0]
        assert listed['status'] == 'ready', listed['error']
        assert listed['parse_info']['text_source'] == 'pdf_tables_v2'
        db = Database(database_url)
        attachments = AttachmentService(db, tmp_path)
        tools = DocumentTools(db, attachments.policy)
        account = owner(database_url, conversation)
        hits = tools.search(UUID(conversation), account, {'query': 'ITEM47'})['results']
        assert any('| ITEM47 | 447.00 | 5.42 |' in hit['content'] for hit in hits)
        read = tools.read(UUID(conversation), account, {'document_id': uploaded['id'], 'start_page': 1, 'end_page': 1},
                          TokenCounter('test'), 10000)
        assert read['complete']
        assert [block['chunk_id'] for block in read['content']] == sorted(block['chunk_id'] for block in read['content'])
        assert any(block['content'].startswith('| Name | Budget | CPC |') for block in read['content'])
        with db.connect() as connection:
            chunks = connection.execute('SELECT * FROM document_chunks WHERE attachment_id=%s ORDER BY ordinal',
                                        (UUID(uploaded['id']),)).fetchall()
            assert all(chunk['token_count'] <= 1000 for chunk in chunks)
            for chunk in chunks:
                if chunk['structure'].get('table'):
                    assert chunk['page'] == chunk['end_page'] == 1
                    assert chunk['content'].startswith('| Name | Budget | CPC |\n| --- | --- | --- |\n')
            # 以虚拟旧解析版本驱动真实恢复，必须清除旧向量而保持原始到期时间。
            connection.execute("UPDATE document_chunks SET embedding=%s,embedding_revision='old' WHERE attachment_id=%s",
                               (Jsonb([1.0, 0.0]), UUID(uploaded['id'])))
            connection.execute("UPDATE attachments SET parse_info=parse_info-'text_source' WHERE id=%s", (UUID(uploaded['id']),))
        attachments.recover()
        recovered = client.get(f'/api/conversations/{conversation}/attachments').json()[0]
        assert recovered['status'] == 'ready', recovered['error']
        assert recovered['expires_at'] == listed['expires_at']
        assert recovered['parse_info']['text_source'] == 'pdf_tables_v2'
        with db.connect() as connection:
            cached = connection.execute('SELECT count(*) AS n FROM document_chunks WHERE attachment_id=%s AND embedding IS NOT NULL',
                                        (UUID(uploaded['id']),)).fetchone()
            assert cached is not None and cached['n'] == 0
            connection.execute("UPDATE document_chunks SET embedding=%s,embedding_revision='current' WHERE attachment_id=%s",
                               (Jsonb([1.0, 0.0]), UUID(uploaded['id'])))
        attachments.recover()
        with db.connect() as connection:
            cached = connection.execute('SELECT count(*) AS n FROM document_chunks WHERE attachment_id=%s AND embedding_revision=%s',
                                        (UUID(uploaded['id']), 'current')).fetchone()
            assert cached is not None and cached['n'] == len(chunks)


def test_table_detection_failure_keeps_text_and_marks_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """表格检测异常不能丢弃仍可提取的正文，应降级纯文字并明确解析缺口。"""
    from general_agent.attachments import documents

    def fail_detection(*args: object) -> None:
        """模拟定位解析器失败，确保走真实 PDF 的纯文字回退路径。"""
        raise ValueError('fixture detection failure')

    path = tmp_path / 'fallback.pdf'
    path.write_bytes(table_pdf())
    monkeypatch.setattr(documents, 'page_units', fail_detection)
    parsed = parse_pdf(path)
    assert parsed.error is None
    assert parsed.info.coverage == 'partial'
    assert '回退纯文字' in ''.join(parsed.info.missing_units)
    assert 'ITEM00' in ''.join(unit.content for unit in parsed.units)


def test_separate_page_tables_keep_unique_ids_and_real_page_numbers(tmp_path: Path) -> None:
    """两页独立表格不能因行列一致而混成同一张表，各片需保留真实物理页。"""
    from pypdf import PdfReader

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(table_pdf())))
    writer.append(PdfReader(BytesIO(table_pdf(ruled=False))))
    output = BytesIO()
    writer.write(output)
    path = tmp_path / 'two-pages.pdf'
    path.write_bytes(output.getvalue())
    parsed = parse_pdf(path)
    assert parsed.error is None
    rows = [unit for unit in parsed.units if unit.table is not None]
    assert {unit.table for unit in rows if unit.page == 1} == {1}
    assert {unit.table for unit in rows if unit.page == 2} == {2}
    assert len(rows) == 18


def test_structure_resource_limit_is_not_hidden_by_text_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """结构解析触及资源硬限制时必须拒绝，不能伪装成可回退的表格检测失败。"""
    from general_agent.attachments import documents

    def exceed_limit(*args: object) -> None:
        """模拟结构块数超过资源上限，验证真实解析入口保留限制错误。"""
        raise ValueError('文档段落数量超过 20,000 个资源限制，请拆分后上传。')

    path = tmp_path / 'resource-limit.pdf'
    path.write_bytes(table_pdf())
    monkeypatch.setattr(documents, 'page_units', exceed_limit)
    parsed = parse_pdf(path)
    assert parsed.error is not None and '资源限制' in parsed.error
