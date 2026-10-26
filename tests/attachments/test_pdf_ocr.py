"""在公开文档解析入口验证扫描页 OCR，外部版面服务使用固定协议替身。"""

from io import BytesIO
from pathlib import Path
from typing import Any
import os

import httpx
from PIL import Image, ImageDraw
import pytest

from general_agent.attachments.documents import parse_document


OCR_REVISION = 'pp-structure-v3-3.3.2-v1'


def scanned_pdf(pages: int = 1) -> bytes:
    """生成整页栅格 PDF，确保真实 PDF 中没有可提取的文字层。"""
    image = Image.new('RGB', (600, 800), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((40, 50), 'SCAN DOCUMENT / RISK R-2048', fill='black')
    output = BytesIO()
    image.save(output, 'PDF', resolution=144, save_all=True, append_images=[image] * (pages - 1))
    image.close()
    return output.getvalue()


def ocr_result(blocks: list[dict[str, Any]], angle: int = 0) -> dict[str, Any]:
    """构造固定版本的外部版面识别协议，坐标对应 600×800 的页面渲染。"""
    return {'revision': OCR_REVISION, 'angle': angle, 'width': 600 if angle % 180 == 0 else 800,
            'height': 800 if angle % 180 == 0 else 600, 'blocks': blocks}


def text_block(text: str, bbox: list[int], label: str = 'text') -> dict[str, Any]:
    """为外部 OCR 替身提供一个有位置、类别和识别置信度的内容块。"""
    return {'label': label, 'bbox': bbox, 'content': text, 'confidence': .98}


def test_scan_ocr_becomes_positioned_body_not_an_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """整页扫描原文必须成为普通正文并定位回物理页，不能伪装成图片描述。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'scan.pdf'
    path.write_bytes(scanned_pdf())

    def provider(request: httpx.Request) -> httpx.Response:
        """外部 OCR 服务返回已知原文，核对只上传页面图片而没有视觉模型凭据。"""
        assert request.url.path == '/parse'
        assert request.headers['content-type'] == 'image/png'
        assert 'authorization' not in request.headers
        assert request.content.startswith(b'\x89PNG')
        return httpx.Response(200, json=ocr_result([
            text_block('设备维护', [40, 40, 400, 90], 'doc_title'),
            text_block('风险编号 R-2048，负责人林女士。', [40, 110, 550, 160]),
        ]))

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None, parsed.error
    assert parsed.info.readable_units == 1
    assert parsed.info.coverage == 'complete'
    assert parsed.info.ocr_completed_pages == [1]
    assert parsed.units[0].kind == 'heading'
    assert parsed.units[1].content.strip() == '风险编号 R-2048，负责人林女士。'
    assert parsed.units[1].regions[0].bbox == pytest.approx((20, 55, 275, 80))
    assert parsed.units[1].section == ['设备维护']
    assert parsed.units[1].text_source == 'ocr'
    assert parsed.images == []


def structured_page() -> dict[str, Any]:
    """提供正文夹插图和合并表头的识别样本，文字顺序与几何上下顺序不同。"""
    return ocr_result([
        text_block('设备维护', [40, 20, 550, 60], 'doc_title'),
        text_block('风险编号 R-2048，负责人林女士。', [320, 100, 560, 160]),
        text_block('多栏下一段正文。', [40, 80, 290, 160]),
        text_block('', [40, 200, 550, 380], 'image'),
        text_block('图 1 设备维护流程', [40, 390, 550, 420], 'figure_title'),
        text_block('FLOW LABEL MUST NOT DUPLICATE', [80, 240, 400, 280]),
        text_block('<table><thead><tr><th colspan="2">维护项目</th></tr>'
                   '<tr><th>项目</th><th>负责人</th></tr></thead>'
                   '<tbody><tr><td>检查密封圈</td><td>林女士</td></tr></tbody></table>',
                   [40, 450, 550, 700], 'table'),
    ])


def test_scan_layout_preserves_table_image_caption_and_reading_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """版面结果中的表格与插图必须独立还原，图内标签和图注不重复进入正文。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'scan.pdf'
    path.write_bytes(scanned_pdf())

    def provider(request: httpx.Request) -> httpx.Response:
        """在 OCR 外部协议边界返回含图文表格的固定版面。"""
        return httpx.Response(200, json=structured_page())

    folder = tmp_path / 'images'
    parsed = parse_document(path, '.pdf', folder, ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None, parsed.error
    body = ''.join(unit.content for unit in parsed.units)
    assert 'FLOW LABEL' not in body and '图 1' not in body
    assert body.index('风险编号') < body.index('多栏下一段')
    rows = [unit for unit in parsed.units if unit.kind == 'table_row']
    assert len(rows) == 3
    assert rows[0].table_cells[0].colspan == 2
    assert rows[0].table_header and rows[1].table_header
    assert '检查密封圈' in rows[-1].content
    assert len(parsed.images) == 1
    image = parsed.images[0]
    assert image.caption == '图 1 设备维护流程'
    assert image.width == 510 and image.height == 180
    assert image.bbox == pytest.approx((20, 100, 275, 190))
    assert image.section == ['设备维护']
    assert image.reading_order == 4
    assert sorted(item.name for item in folder.iterdir()) == [image.storage_name]


def test_uploaded_scan_keeps_body_table_and_image_as_separate_chunks(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """附件公开上传后，OCR 原文与表格可检索，插图独立描述并按阅读顺序返回。"""
    from uuid import UUID
    from general_agent.attachments.service import AttachmentService
    from general_agent.infrastructure.database import Database
    from general_agent.infrastructure.tokens import TokenCounter
    from tests.attachments.test_attachments import configure, configure_settings
    from tests.attachments.test_document_images import image_files_response, wait_ready
    from tests.attachments.test_knowledge import owner
    from tests.auth.support import authenticated_client

    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    descriptions: list[str] = []
    original_pdf = scanned_pdf()

    def provider(request: httpx.Request) -> httpx.Response:
        """OCR 只还原结构，视觉模型仅收到裁出的插图与图注。"""
        if request.url.path == '/parse':
            return httpx.Response(200, json=structured_page())
        if request.url.path == '/files':
            return image_files_response()
        import json
        payload = json.loads(request.content)
        prompt = payload['messages'][0]['content'][0]['text']
        assert '图 1 设备维护流程' in prompt
        descriptions.append(prompt)
        return httpx.Response(200, json={'choices': [{'message': {'content': '设备维护流程图显示巡检后更换密封圈。'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('scan.pdf', original_pdf)}).json()[0]
        final = wait_ready(client, conversation, uploaded['id'])
        assert final['status'] == 'ready', final
        assert final['parse_info']['ocr_completed_pages'] == [1]
        service = AttachmentService(Database(database_url), tmp_path)
        account = owner(database_url, conversation)
        hits = service.documents.search(UUID(conversation), account, {'query': 'R-2048'})['results']
        body = next(hit for hit in hits if '风险编号' in hit['content'])
        assert body['image_id'] is None
        assert body['structure']['text_source'] == 'ocr'
        read = service.documents.read(UUID(conversation), account,
            {'document_id': uploaded['id'], 'start_page': 1, 'end_page': 1}, TokenCounter('test'), 10000)
        blocks = read['content']
        contents = [item['content'] for item in blocks]
        assert len(descriptions) == 1
        assert '风险编号' in contents[0] and contents[0].index('风险编号') < contents[0].index('多栏下一段')
        assert '流程图显示' in contents[1] and blocks[1]['image_id']
        assert '检查密封圈' in contents[2] and blocks[2]['structure']['table']['columns'] == 2
        assert client.get(f'/api/conversations/{conversation}/attachments/{uploaded["id"]}/file').content == original_pdf


def test_scan_rotation_maps_regions_back_to_original_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """旋转后识别的插图必须正确裁剪，定位框逆映射回原 PDF 而不是旋转画布。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'rotated.pdf'
    path.write_bytes(scanned_pdf())

    def provider(request: httpx.Request) -> httpx.Response:
        """返回逆时针旋转九十度后的文本及插图位置。"""
        return httpx.Response(200, json=ocr_result([
            text_block('旋转正文', [40, 40, 400, 90]),
            text_block('', [100, 200, 300, 400], 'image'),
        ], angle=90))

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None, parsed.error
    assert parsed.units[0].regions[0].bbox == pytest.approx((255, 20, 280, 200))
    assert parsed.images[0].bbox == pytest.approx((100, 50, 200, 150))
    assert (parsed.images[0].width, parsed.images[0].height) == (200, 200)


def test_scan_page_failure_keeps_other_pages_and_marks_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第二页识别失败时仍可使用第一页，失败页不能变成整页图片描述。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'two.pdf'
    path.write_bytes(scanned_pdf(2))
    requests = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """模拟逐页识别先成功、后超时。"""
        nonlocal requests
        requests += 1
        if requests == 2:
            raise httpx.ReadTimeout('timeout')
        return httpx.Response(200, json=ocr_result([text_block('第一页正文', [40, 40, 550, 120])]))

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None
    assert parsed.info.readable_units == 1 and parsed.info.coverage == 'partial'
    assert parsed.info.ocr_completed_pages == [1] and parsed.info.ocr_failed_pages == [2]
    assert '第 2 页' in str(parsed.info.missing_units)
    assert parsed.images == []
    assert list((tmp_path / 'images').iterdir()) == []


def test_unconfigured_ocr_reports_scan_failure_but_text_pdf_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未部署 OCR 时扫描件明确失败，正常文字 PDF 无需 OCR 服务即可继续使用。"""
    from tests.attachments.test_pdf_tables import positioned_pdf
    monkeypatch.delenv('DOCUMENT_OCR_URL', raising=False)
    path = tmp_path / 'scan.pdf'
    path.write_bytes(scanned_pdf())
    parsed = parse_document(path, '.pdf', tmp_path / 'images')
    assert parsed.error and '未配置' in parsed.error
    assert parsed.images == []
    path.write_bytes(positioned_pdf(['BT /F1 12 Tf 30 700 Td (NORMAL TEXT) Tj ET']))
    text = parse_document(path, '.pdf', tmp_path / 'native-images')
    assert text.error is None
    assert 'NORMAL TEXT' in ''.join(unit.content for unit in text.units)
    assert text.info.ocr_rules is None


def test_knowledge_scan_citations_keep_text_table_and_image_types(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """知识库公开上传与对话检索保留三类来源，OCR 正文引用不能被误当作整页图片。"""
    import json
    from tests.attachments.test_attachments import configure_settings
    from tests.attachments.test_document_images import image_files_response
    from tests.auth.support import authenticated_client
    from tests.knowledge.test_knowledge_base import wait_document
    from tests.knowledge.test_retrieval import tool_response

    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    results: list[dict[str, Any]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """在独立 OCR、视觉描述与回答模型三个外部边界提供固定结果。"""
        if request.url.path == '/parse':
            return httpx.Response(200, json=structured_page())
        if request.url.path == '/files':
            return image_files_response()
        payload = json.loads(request.content)
        first = payload['messages'][0].get('content')
        if isinstance(first, list):
            assert '图 1 设备维护流程' in first[0]['text']
            return httpx.Response(200, json={'choices': [{'message': {'content': '设备维护流程图显示巡检后更换密封圈。'}}]})
        tool_messages = [message for message in payload['messages'] if message['role'] == 'tool']
        if not tool_messages:
            return tool_response('设备 维护 R-2048 林女士 密封圈')
        result = json.loads(tool_messages[-1]['content'])
        results.append(result)
        answer = '\n\n'.join('来源 [' + item['key'] + ']' for item in result['results'])
        return httpx.Response(200, json={'choices': [{'message': {'content': answer}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '扫描资料'}).json()['id']
        uploaded = client.post(f'/api/knowledge-bases/{base}/documents',
                               files={'files': ('scan.pdf', scanned_pdf())}).json()[0]
        final = wait_document(client, base, uploaded['id'])
        assert final['status'] == 'ready', final
        assert final['parse_info']['ocr_completed_pages'] == [1]
        conversation = client.post('/api/conversations', json={}).json()['id']
        client.put(f'/api/conversations/{conversation}/knowledge-bases', json={'knowledge_base_ids': [base]})
        reply = client.post(f'/api/conversations/{conversation}/messages', json={'content': '设备维护的风险与处理方式？'})
        assert reply.status_code == 200, reply.text
        from general_agent.knowledge.tools import KnowledgeTools
        assert {KnowledgeTools.source_kind(item) for item in results[-1]['results']} == {'text', 'table', 'image'}
        sources = reply.json()['messages'][-1]['knowledge_sources']
        assert {source['kind'] for source in sources} == {'text', 'table', 'image'}
        assert all(source['page'] == 1 for source in sources)


def test_low_confidence_scan_is_readable_but_reports_quality_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """低置信度识别保留正文用于核对，同时标记具体页，不能声称完整识别。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'uncertain.pdf'
    path.write_bytes(scanned_pdf())

    def provider(request: httpx.Request) -> httpx.Response:
        """模拟 OCR 服务识别到可读内容但整体置信度偏低。"""
        result = ocr_result([text_block('可能含错字的扫描原文', [40, 40, 550, 120])])
        result['confidence'] = .55
        return httpx.Response(200, json=result)

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None
    assert parsed.info.coverage == 'partial'
    assert parsed.info.ocr_low_confidence_pages == [1]
    assert '可能含错字' in parsed.units[0].content


def test_scan_table_without_reliable_header_uses_local_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有可靠表头的扫描表格保留纯文字和截图，且不送视觉模型改写原始数字。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'ambiguous.pdf'
    path.write_bytes(scanned_pdf())

    def provider(request: httpx.Request) -> httpx.Response:
        """返回只有数据单元格的表格，要求应用保守降级而非猜测表头。"""
        return httpx.Response(200, json=ocr_result([text_block(
            '<table><tr><td>100.25</td><td>200.50</td></tr></table>',
            [40, 100, 550, 500], 'table',
        )]))

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None
    assert parsed.info.coverage == 'partial'
    assert parsed.images[0].media_type == 'table_preview'
    assert parsed.units[0].table_image_id == parsed.images[0].id
    assert '100.25' in parsed.units[0].content and '200.50' in parsed.units[0].content


def test_mixed_pdf_uses_ocr_only_for_full_scan_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """混合 PDF 的文字页保留原生结构，只有扫描页发给 OCR，物理页号不能错位。"""
    from pypdf import PdfReader, PdfWriter
    from tests.attachments.test_pdf_tables import positioned_pdf
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    output = BytesIO()
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(positioned_pdf(['BT /F1 12 Tf 30 700 Td (NATIVE PAGE) Tj ET']))))
    writer.append(PdfReader(BytesIO(scanned_pdf())))
    writer.write(output)
    path = tmp_path / 'mixed.pdf'
    path.write_bytes(output.getvalue())
    requests = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """只允许扫描页请求一次，返回第二页正文。"""
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=ocr_result([text_block('第二页扫描原文', [40, 40, 550, 120])]))

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None
    assert requests == 1
    assert parsed.info.total_units == parsed.info.readable_units == 2
    assert parsed.info.ocr_completed_pages == [2]
    assert parsed.units[0].page == 1 and 'NATIVE PAGE' in parsed.units[0].content
    assert parsed.units[1].page == 2 and '扫描原文' in parsed.units[1].content


@pytest.mark.skipif(not os.environ.get('OCR_ACCEPTANCE_URL'), reason='设置 OCR_ACCEPTANCE_URL 后验证真实 PP-StructureV3。')
def test_real_structure_ocr_reads_synthetic_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实模型从合成印刷体扫描件提取独立哨兵文字，不向外部聊天模型发送文件。"""
    import os
    from PIL import ImageFont
    monkeypatch.setenv('DOCUMENT_OCR_URL', os.environ['OCR_ACCEPTANCE_URL'])
    source = Image.new('RGB', (1200, 1600), 'white')
    draw = ImageDraw.Draw(source)
    font = ImageFont.load_default(size=36)
    draw.text((80, 100), 'EQUIPMENT MAINTENANCE REPORT', font=font, fill='black')
    draw.text((80, 240), 'Risk number: R-2048', font=font, fill='black')
    draw.text((80, 320), 'Inspection interval: every 30 days.', font=font, fill='black')
    draw.text((80, 400), 'Replace the seal after inspection.', font=font, fill='black')
    path = tmp_path / 'real-scan.pdf'
    source.save(path, 'PDF', resolution=144)
    source.close()
    parsed = parse_document(path, '.pdf', tmp_path / 'images')
    assert parsed.error is None, parsed.error
    text = ''.join(unit.content for unit in parsed.units)
    assert 'R-2048' in text, text
    assert '30' in text, text
    assert parsed.info.ocr_completed_pages == [1]
    assert all(unit.regions and unit.page == 1 for unit in parsed.units)


def test_unique_page_edge_note_is_not_silently_discarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只有重复确认的页眉页脚才清理，单页的重要页边备注必须仍可检索。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'note.pdf'
    path.write_bytes(scanned_pdf())

    def provider(request: httpx.Request) -> httpx.Response:
        """返回唯一页脚备注与普通正文，确保不能仅凭模型标签删除原文。"""
        return httpx.Response(200, json=ocr_result([
            text_block('设备正文', [40, 60, 550, 150]),
            text_block('仅适用于低温环境', [40, 740, 550, 780], 'footer'),
        ]))

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None
    assert '仅适用于低温环境' in ''.join(unit.content for unit in parsed.units)


def test_deleting_knowledge_during_ocr_does_not_publish_late_text(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCR 请求尚未返回时删除知识库文档，迟到原文不能重新发布或恢复文件访问。"""
    from threading import Event
    from time import sleep
    from tests.auth.support import authenticated_client
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    entered, release = Event(), Event()

    def provider(request: httpx.Request) -> httpx.Response:
        """阻塞独立 OCR 请求直到删除发生，再返回原本可用的正文。"""
        assert request.url.path == '/parse'
        entered.set()
        assert release.wait(15)
        return httpx.Response(200, json=ocr_result([text_block('迟到 OCR 原文', [40, 40, 550, 120])]))

    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': '删除 OCR 文档'}).json()['id']
        try:
            document = client.post(f'/api/knowledge-bases/{base}/documents',
                                   files={'files': ('scan.pdf', scanned_pdf())}).json()[0]
            assert entered.wait(10)
            assert client.delete(f'/api/knowledge-bases/{base}/documents/{document["id"]}').status_code == 200
            assert client.get(f'/api/knowledge-bases/{base}/documents/{document["id"]}/file').status_code == 404
        finally:
            release.set()
        for _ in range(100):
            if not client.get(f'/api/knowledge-bases/{base}/documents').json():
                break
            sleep(.02)
        assert client.get(f'/api/knowledge-bases/{base}/documents').json() == []


def test_long_scanned_table_chunks_repeat_all_header_rows(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫描长表按行切片时每片都携带完整多级表头，不能只保留第一层标题。"""
    from uuid import UUID
    from general_agent.attachments.service import AttachmentService
    from general_agent.infrastructure.database import Database
    from general_agent.infrastructure.tokens import TokenCounter
    from tests.attachments.test_attachments import configure, configure_settings
    from tests.attachments.test_document_images import wait_ready
    from tests.attachments.test_knowledge import owner
    from tests.auth.support import authenticated_client
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    monkeypatch.setenv('DOCUMENT_CHUNK_TOKENS', '120')
    monkeypatch.setenv('DOCUMENT_CHUNK_OVERFLOW_TOKENS', '0')
    table = '<table><thead><tr><th colspan="2">检查清单</th></tr><tr><th>项目</th><th>编号</th></tr></thead><tbody>'
    table += ''.join(f'<tr><td>密封圈检查</td><td>ITEM-{index:02d}</td></tr>' for index in range(20))
    table += '</tbody></table>'

    def provider(request: httpx.Request) -> httpx.Response:
        """返回真实长表对应的固定 OCR 外部结构，不调用视觉描述。"""
        assert request.url.path == '/parse'
        return httpx.Response(200, json=ocr_result([text_block(table, [40, 40, 550, 760], 'table')]))

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('long.pdf', scanned_pdf())}).json()[0]
        final = wait_ready(client, conversation, uploaded['id'])
        assert final['status'] == 'ready', final
        service = AttachmentService(Database(database_url), tmp_path)
        contents: list[str] = []
        arguments: dict[str, Any] = {'document_id': uploaded['id']}
        while True:
            result = service.documents.read(UUID(conversation), owner(database_url, conversation),
                                            arguments, TokenCounter('test'), 10000)
            contents.extend(item['content'] for item in result['content'])
            if result['next_cursor'] is None:
                break
            arguments['cursor'] = result['next_cursor']
        assert len(contents) > 1
        assert all('检查清单' in content and '| 项目 | 编号 |' in content for content in contents)
        assert sum(content.count('ITEM-19') for content in contents) == 1


def test_full_page_image_prediction_keeps_ocr_body_and_never_becomes_figure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型误把整页判为插图时保留已有 OCR 正文，并明确拒绝整页视觉描述。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'misclassified.pdf'
    path.write_bytes(scanned_pdf())

    def provider(request: httpx.Request) -> httpx.Response:
        """返回覆盖整个扫描页的误判图片区与独立正文结果。"""
        return httpx.Response(200, json=ocr_result([
            text_block('', [0, 0, 600, 800], 'image'),
            text_block('风险编号 R-2048', [40, 60, 550, 120]),
        ]))

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None
    assert parsed.images == []
    assert 'R-2048' in ''.join(unit.content for unit in parsed.units)
    assert parsed.info.coverage == 'partial'
    assert '整页' in str(parsed.info.missing_units)


def test_bound_caption_does_not_remove_same_text_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """图注去重仅移除实际绑定的版面块，其他位置相同的文字仍是普通正文。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'same-text.pdf'
    path.write_bytes(scanned_pdf())

    def provider(request: httpx.Request) -> httpx.Response:
        """在页首正文与插图下方图注中放入相同文字以暴露按文字去重的误删。"""
        return httpx.Response(200, json=ocr_result([
            text_block('图 1 设备维护流程', [40, 40, 550, 80]),
            text_block('', [40, 200, 550, 380], 'image'),
            text_block('图 1 设备维护流程', [40, 390, 550, 420], 'figure_title'),
        ]))

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None
    assert ''.join(unit.content for unit in parsed.units).count('图 1 设备维护流程') == 1
    assert parsed.images[0].caption == '图 1 设备维护流程'


def test_long_table_preview_remains_chunkable_and_readable(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无法确定表头的长表仍按预算切片，每片保留截图绑定并能续读到末行。"""
    from uuid import UUID
    from general_agent.attachments.service import AttachmentService
    from general_agent.infrastructure.database import Database
    from general_agent.infrastructure.tokens import TokenCounter
    from tests.attachments.test_attachments import configure, configure_settings
    from tests.attachments.test_document_images import wait_ready
    from tests.attachments.test_knowledge import owner
    from tests.auth.support import authenticated_client
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    table = '<table>' + ''.join(f'<tr><td>维护项目 ITEM-{i:03d}</td><td>{i}.25</td></tr>' for i in range(150)) + '</table>'

    def provider(request: httpx.Request) -> httpx.Response:
        """返回超过单片预算、没有显式表头的真实形态 OCR 表格。"""
        assert request.url.path == '/parse'
        return httpx.Response(200, json=ocr_result([text_block(table, [40, 40, 550, 760], 'table')]))

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('preview.pdf', scanned_pdf())}).json()[0]
        final = wait_ready(client, conversation, uploaded['id'])
        assert final['status'] == 'ready', final
        service = AttachmentService(Database(database_url), tmp_path)
        arguments: dict[str, Any] = {'document_id': uploaded['id']}
        chunks: list[dict[str, Any]] = []
        while True:
            result = service.documents.read(UUID(conversation), owner(database_url, conversation),
                                            arguments, TokenCounter('test'), 10000)
            chunks.extend(result['content'])
            if result['next_cursor'] is None:
                break
            arguments['cursor'] = result['next_cursor']
        assert len(chunks) > 1
        assert 'ITEM-149' in ''.join(chunk['content'] for chunk in chunks)
        for chunk in chunks:
            assert service.policy.counter.count(chunk['content']) <= service.policy.max_tokens
            fragment = chunk['structure']['table']
            assert fragment['render_as_image'] and fragment['image_id']
            assert fragment['cells'][0]['text'] == chunk['content']


def test_rotated_independent_tables_are_not_merged_as_continuations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两页旋转扫描件的原页上下边缘不代表阅读边缘，独立同表头表格不能被合并。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'rotated-tables.pdf'
    path.write_bytes(scanned_pdf(2))
    requests = 0
    table = '<table><tr><th>项目</th><th>编号</th></tr><tr><td>密封圈</td><td>1</td></tr></table>'

    def provider(request: httpx.Request) -> httpx.Response:
        """第一页阅读顶部和第二页阅读底部的表格经逆映射后正好落在相反原页边缘。"""
        nonlocal requests
        requests += 1
        box = [40, 40, 550, 120] if requests == 1 else [40, 680, 550, 760]
        return httpx.Response(200, json=ocr_result([text_block(table, box, 'table')], angle=180))

    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error is None
    assert {unit.table for unit in parsed.units} == {1, 2}
    assert len([unit for unit in parsed.units if unit.table_header and unit.content.strip()]) == 2


def test_figure_limit_is_enforced_before_extra_crops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超出插图数量限制时在第 51 次裁剪前停止，不能先编码写盘成千上万张图。"""
    monkeypatch.setenv('DOCUMENT_OCR_URL', 'http://ocr.example')
    path = tmp_path / 'many-figures.pdf'
    path.write_bytes(scanned_pdf())
    original_crop = Image.Image.crop
    crops = 0

    def counted_crop(source: Image.Image, box: Any = None) -> Image.Image:
        """在第三方图像库边界统计实际像素裁剪次数，防止资源限制只在写盘后检查。"""
        nonlocal crops
        crops += 1
        return original_crop(source, box)

    def provider(request: httpx.Request) -> httpx.Response:
        """返回数量超过限制但仍在合法协议范围内的插图列表。"""
        return httpx.Response(200, json=ocr_result([text_block('', [40, 200, 80, 240], 'image')] * 51))

    monkeypatch.setattr(Image.Image, 'crop', counted_crop)
    parsed = parse_document(path, '.pdf', tmp_path / 'images', ocr_transport=httpx.MockTransport(provider))
    assert parsed.error and '资源限制' in parsed.error
    assert crops <= 50
    assert list((tmp_path / 'images').iterdir()) == []
