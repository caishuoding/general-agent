"""通过真实 PDF 解析验收表格合并关系、表头、表题及跨页衔接。"""

from pathlib import Path
from io import BytesIO

from pypdf import PdfReader, PdfWriter
from pypdf.generic import StreamObject

from general_agent.attachments.documents import parse_pdf
from tests.attachments.test_pdf_tables import positioned_pdf, table_pdf


def cross_page_table_bytes(continued_start: int = 20) -> bytes:
    """构造前页抵近页尾、后页从顶部重复表头的续表；可指定第二页首行区分数据。"""
    writer = PdfWriter()
    for page_number, start in ((1, 0), (2, continued_start)):
        commands: list[str] = []
        top, height = 820.0, 24.0
        rows = 31 if page_number == 1 else 8
        for row in range(rows + 1):
            y = top - row * height
            commands.append(f'30 {y} m 470 {y} l S')
        for x in (30, 190, 330, 470):
            commands.append(f'{x} {top} m {x} {top-rows*height} l S')
        values = [['Name', 'Budget', 'CPC']] + [[f'ROW{index:02d}', str(index), '5.4'] for index in range(start, start + rows - 1)]
        for row, cells in enumerate(values):
            for x, value in zip((35, 195, 335), cells):
                commands.append(f'BT /F1 10 Tf {x} {top-row*height-15} Td ({value}) Tj ET')
        writer.append(PdfReader(BytesIO(positioned_pdf(commands))))
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def multilevel_table_pdf() -> bytes:
    """绘制含整行表题、两级表头、跨行表头及跨行数据格的真实线框表格。"""
    commands = ['30 820 m 470 820 l S', '30 796 m 470 796 l S', '30 772 m 330 772 l S',
                '30 748 m 470 748 l S', '190 724 m 470 724 l S', '30 700 m 470 700 l S',
                '30 820 m 30 700 l S', '470 820 m 470 700 l S',
                '190 772 m 190 700 l S', '330 796 m 330 700 l S']
    for x, y, text in [(35, 804, 'Table 2 Release costs'), (35, 780, 'Order metrics'), (335, 766, 'CPC'),
                       (35, 756, 'Name'), (195, 756, 'Budget'), (35, 728, 'Order A'),
                       (195, 732, '100'), (335, 732, '5.4'), (195, 708, '200'), (335, 708, '6.1')]:
        commands.append(f'BT /F1 10 Tf {x} {y} Td ({text}) Tj ET')
    return positioned_pdf(commands)


def test_merged_table_keeps_real_column_spans(tmp_path: Path) -> None:
    """合并表头必须保存真实跨列关系，后续展示不能只依靠空字符串占位。"""
    path = tmp_path / 'merged.pdf'
    path.write_bytes(table_pdf(merged=True))
    parsed = parse_pdf(path)
    assert parsed.error is None
    header = next(unit for unit in parsed.units if unit.table is not None)
    assert header.table_columns == 3
    assert header.table_header
    assert [(cell.column, cell.colspan, cell.text) for cell in header.table_cells] == [
        (0, 2, 'Name and Budget'), (2, 1, 'CPC'),
    ]
    assert all(cell.header for cell in header.table_cells)


def test_table_title_binds_to_table_and_is_removed_from_body(tmp_path: Path) -> None:
    """独立表题归属表格并与表头一起切片，不能留在相邻普通正文中。"""
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(table_pdf())))
    stream = writer.pages[0]['/Contents'].get_object()
    assert isinstance(stream, StreamObject)
    stream.set_data(stream.get_data() + b'\nBT /F1 11 Tf 30 835 Td (Table 1 Budget plan) Tj ET')
    path = tmp_path / 'title.pdf'
    writer.write(path)
    parsed = parse_pdf(path)
    assert parsed.error is None
    rows = [unit for unit in parsed.units if unit.table is not None]
    assert rows[0].table_title == 'Table 1 Budget plan'
    assert rows[0].content.startswith('Table 1 Budget plan\n\n| Name | Budget | CPC |')
    assert all('Table 1 Budget plan' not in unit.content for unit in parsed.units if unit.table is None)


def test_multilevel_headers_skip_title_and_keep_rowspans(tmp_path: Path) -> None:
    """表格内部整行表题不算表头，两级表头及数据跨行关系均应保留。"""
    path = tmp_path / 'multilevel.pdf'
    path.write_bytes(multilevel_table_pdf())
    parsed = parse_pdf(path)
    assert parsed.error is None
    rows = [unit for unit in parsed.units if unit.table is not None]
    assert len(rows) == 4
    assert rows[0].table_title == 'Table 2 Release costs'
    assert [unit.table_header for unit in rows] == [True, True, False, False]
    cells = [cell for unit in rows for cell in unit.table_cells]
    assert [(cell.row, cell.column, cell.rowspan, cell.colspan) for cell in cells if cell.text == 'CPC'] == [(0, 2, 2, 1)]
    assert [(cell.row, cell.rowspan, cell.header) for cell in cells if cell.text == 'Order A'] == [(2, 2, False)]


def test_cross_page_table_merges_only_with_repeated_header_and_edge_evidence(tmp_path: Path) -> None:
    """抵近页尾且下一页重复表头时保留同一表格身份，并标记续表起点。"""
    path = tmp_path / 'continued-table.pdf'
    path.write_bytes(cross_page_table_bytes())
    parsed = parse_pdf(path)
    assert parsed.error is None
    rows = [unit for unit in parsed.units if unit.table is not None]
    assert len({unit.table for unit in rows}) == 1
    continuation = [unit for unit in rows if unit.table_continued]
    assert len(continuation) == 1
    assert continuation[0].page == 2 and continuation[0].table_header


def test_cross_page_table_fragment_keeps_each_data_row_once(database_url: str, tmp_path: Path) -> None:
    """上传跨页续表后，阅读结果必须保留第二页每行且不能用重复行号覆盖第一页数据。"""
    from uuid import UUID
    from general_agent.attachments.service import AttachmentService
    from general_agent.attachments.tools import DocumentTools
    from general_agent.infrastructure.database import Database
    from general_agent.infrastructure.tokens import TokenCounter
    from tests.attachments.test_attachments import configure, configure_settings
    from tests.attachments.test_document_images import wait_ready
    from tests.attachments.test_knowledge import owner
    from tests.auth.support import authenticated_client

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('continued.pdf', cross_page_table_bytes(30))}).json()[0]
        ready = wait_ready(client, conversation, uploaded['id'])
        assert ready['status'] == 'ready', ready['error']
        db = Database(database_url)
        tools = DocumentTools(db, AttachmentService(db, tmp_path).policy)
        read = tools.read(UUID(conversation), owner(database_url, conversation),
                          {'document_id': uploaded['id'], 'start_page': 1, 'end_page': 2},
                          TokenCounter('test'), 10000)
        assert 'content' in read, read
        blocks = {block['chunk_id']: block for block in read['content']}
        table_blocks = [block for block in blocks.values() if block.get('structure', {}).get('table')]
        cells = [cell for block in table_blocks for cell in block['structure']['table']['cells']]
        assert all(len({(cell['row'], cell['column']) for cell in block['structure']['table']['cells']})
                   == len(block['structure']['table']['cells']) for block in table_blocks)
        assert {value: sum(cell['text'] == value for cell in cells)
                for value in ('ROW29', 'ROW30', 'ROW36')} == {
                    'ROW29': 1, 'ROW30': 1, 'ROW36': 1,
                }


def test_nearby_independent_tables_keep_distinct_ids(tmp_path: Path) -> None:
    """相邻页表格未抵近页尾时即使表头相同也不能误判为跨页续表。"""
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(table_pdf())))
    writer.append(PdfReader(BytesIO(table_pdf())))
    path = tmp_path / 'independent.pdf'
    writer.write(path)
    parsed = parse_pdf(path)
    assert parsed.error is None
    rows = [unit for unit in parsed.units if unit.table is not None]
    assert len({unit.table for unit in rows}) == 2
    assert not any(unit.table_continued for unit in rows)


def test_ambiguous_numeric_table_keeps_region_screenshot_fallback(tmp_path: Path) -> None:
    """无法从文字区分表头的数据矩阵应保留原区域截图，并由表格切片绑定该图片。"""
    commands: list[str] = []
    top, height = 700, 25
    for row in range(5):
        commands.append(f'30 {top-row*height} m 330 {top-row*height} l S')
    for x in (30, 180, 330):
        commands.append(f'{x} {top} m {x} {top-4*height} l S')
    for row, values in enumerate((['12', '18'], ['20', '25'], ['31', '42'], ['44', '58'])):
        for x, value in zip((35, 185), values):
            commands.append(f'BT /F1 10 Tf {x} {top-row*height-16} Td ({value}) Tj ET')
    path = tmp_path / 'numeric-table.pdf'
    path.write_bytes(positioned_pdf(commands))
    parsed = parse_pdf(path, tmp_path / 'images')
    assert parsed.error is None
    previews = [image for image in parsed.images if image.media_type == 'table_preview']
    assert len(previews) == 1
    assert (tmp_path / 'images' / previews[0].storage_name).is_file()
    rows = [unit for unit in parsed.units if unit.table is not None]
    assert rows and all(unit.table_image_id == previews[0].id for unit in rows)
    assert rows[0].table_header is False


def test_ambiguous_table_preview_is_persisted_without_visual_model(database_url: str, tmp_path: Path) -> None:
    """数据矩阵兜底截图完成本地入库，检索结果引用截图身份且不调用外部视觉模型。"""
    from uuid import UUID
    import httpx
    from general_agent.attachments.service import AttachmentService
    from general_agent.attachments.tools import DocumentTools
    from general_agent.infrastructure.database import Database
    from tests.attachments.test_attachments import configure, configure_settings
    from tests.attachments.test_document_images import wait_ready
    from tests.attachments.test_knowledge import owner
    from tests.auth.support import authenticated_client

    def provider(request: httpx.Request) -> httpx.Response:
        """任何外部模型请求都失败，证明表格截图兜底只在本地完成。"""
        raise AssertionError(f'不应调用外部模型：{request.url}')

    commands = ['30 700 m 330 700 l S', '30 675 m 330 675 l S', '30 650 m 330 650 l S',
                '30 700 m 30 650 l S', '180 700 m 180 650 l S', '330 700 m 330 650 l S',
                'BT /F1 10 Tf 35 684 Td (12) Tj ET', 'BT /F1 10 Tf 185 684 Td (18) Tj ET',
                'BT /F1 10 Tf 35 659 Td (20) Tj ET', 'BT /F1 10 Tf 185 659 Td (25) Tj ET']
    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('matrix.pdf', positioned_pdf(commands))}).json()[0]
        ready = wait_ready(client, conversation, uploaded['id'])
        assert ready['status'] == 'ready', ready['error']
        assert ready['parse_info']['image_count'] == 1
        db = Database(database_url)
        tools = DocumentTools(db, AttachmentService(db, tmp_path).policy)
        hits = tools.search(UUID(conversation), owner(database_url, conversation), {'query': '12'})['results']
        table = next(hit for hit in hits if hit.get('structure', {}).get('table'))['structure']['table']
        assert table['render_as_image'] is True
        assert table['image_id']


def test_cross_page_continuation_reuses_first_header_once(tmp_path: Path) -> None:
    """跨页续表分片只保留首份表头，不把续页重复表头当作数据行。"""
    from general_agent.attachments.knowledge import ChunkPolicy, chunk_specs

    path = tmp_path / 'continued-chunk.pdf'
    path.write_bytes(cross_page_table_bytes())
    parsed = parse_pdf(path)
    assert parsed.error is None
    text = ''.join(unit.content for unit in parsed.units)
    contents = [spec.prefix + text[spec.start:spec.end] + spec.suffix
                for spec in chunk_specs(parsed.units, ChunkPolicy())]
    assert contents
    assert sum(content.count('| Name | Budget | CPC |') for content in contents) == 1
