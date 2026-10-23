"""通过真实 PDF 解析入口验收单栏文档结构，不依赖解析器内部辅助函数。"""

import os
import json
from io import BytesIO
from pathlib import Path

import pytest
import httpx
import psycopg
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject
from pdfplumber.page import Page

from general_agent.attachments.documents import parse_pdf
from tests.attachments.test_pdf_tables import positioned_pdf


@pytest.fixture
def architecture_pdf() -> Path:
    """使用用户提供的架构文档验收，不把本机原件复制到仓库或固定绝对路径。"""
    configured = os.environ.get('PDF_ACCEPTANCE_SAMPLE')
    if not configured:
        pytest.skip('设置 PDF_ACCEPTANCE_SAMPLE 后执行真实架构文档验收。')
    path = Path(configured)
    assert path.is_file(), 'PDF_ACCEPTANCE_SAMPLE 必须指向用户提供的架构文档。'
    return path


def test_sample_recovers_heading_hierarchy_and_paragraphs(architecture_pdf: Path) -> None:
    """第一页需恢复章节层级和自然段，正文不能保留打断中文词语的排版换行。"""
    parsed = parse_pdf(architecture_pdf)
    assert parsed.error is None
    first = [unit for unit in parsed.units if unit.page == 1]
    headings = [unit for unit in first if unit.kind == 'heading']
    assert [(unit.content.strip(), unit.heading_level) for unit in headings] == [
        ('01 业务边界与领域上下文', 1),
        ('目标与约束', 2),
        ('设计与落地细节', 2),
        ('异常场景与处置原则', 2),
        ('验证、交付与运维要求', 2),
    ]
    paragraph = next(unit for unit in first if unit.content.startswith('本节针对'))
    assert '业务输入由交易域接收' in paragraph.content
    assert '\n' not in paragraph.content.strip()
    assert paragraph.section == ['01 业务边界与领域上下文', '目标与约束']


def test_sample_removes_repeated_margins_without_losing_body(architecture_pdf: Path) -> None:
    """跨页重复页边内容不进入正文，正文中的章节引用和业务数字仍须保留。"""
    parsed = parse_pdf(architecture_pdf)
    assert parsed.error is None
    text = ''.join(unit.content for unit in parsed.units)
    assert '合成技术语料' not in text
    assert 'R4.2 / 2026-09' not in text
    assert 'TECHNICAL STANDARD' not in text
    assert '入口流量峰值 18.5k RPS' in text
    assert '与上一节“业务边界与领域上下文”的接口关系' in text
    assert len([unit for unit in parsed.units if unit.heading_level == 1]) == 20


def test_sample_keeps_identifiers_metadata_rows_and_captions(architecture_pdf: Path) -> None:
    """恢复被换行截断的中英文标识符，独立元信息行不粘连，图注绑定后移出正文。"""
    parsed = parse_pdf(architecture_pdf)
    assert parsed.error is None
    second = [unit for unit in parsed.units if unit.page == 2]
    paragraph = next(unit for unit in second if unit.content.startswith('本节针对'))
    assert '业务输入由Ingress接收' in paragraph.content
    metadata = [unit.content.strip() for unit in second if unit.content.startswith(('适用组件', '核心原则'))]
    assert metadata == ['适用组件 Ingress / API Gateway / 边缘鉴权', '核心原则 就近接入与隔离故障域']
    assert [(image.page, image.caption) for image in parsed.images] == [
        (2, '图 2-1 入口请求与服务隔离路径'),
        (5, '图 5-1 异步事件路由与死信归档'),
        (8, '图 8-1 预占回收及补偿状态机'),
        (11, '图 11-1 结算链路依赖与降级许可表'),
        (14, '图 14-1 灰度发布的渐进式放量'),
        (17, '图 17-1 关键路径五分钟监控截面'),
    ]
    assert all(image.caption is not None and image.caption not in ''.join(unit.content for unit in parsed.units)
               for image in parsed.images)


def test_bookmarked_title_and_list_items_keep_positions(tmp_path: Path) -> None:
    """同字号书签标题可识别，列表续行归属原列表项且各块位置符合页面阅读顺序。"""
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(positioned_pdf([
        'BT /F1 11 Tf 30 840 Td (Release checklist) Tj ET',
        'BT /F1 11 Tf 30 800 Td (1. Prepare request) Tj ET',
        'BT /F1 11 Tf 45 785 Td (and keep evidence.) Tj ET',
        'BT /F1 11 Tf 30 760 Td (2. Confirm release.) Tj ET',
    ]))))
    writer.add_outline_item('Release checklist', 0)
    path = tmp_path / 'list.pdf'
    writer.write(path)
    parsed = parse_pdf(path)
    assert parsed.error is None
    assert [(unit.kind, unit.content.strip()) for unit in parsed.units] == [
        ('heading', 'Release checklist'),
        ('paragraph', '1. Prepare request and keep evidence.'),
        ('paragraph', '2. Confirm release.'),
    ]
    assert all(unit.section == ['Release checklist'] for unit in parsed.units)
    assert all(len(unit.regions) == 1 and unit.regions[0].page == 1 for unit in parsed.units)
    assert [unit.regions[0].bbox[1] for unit in parsed.units] == sorted(unit.regions[0].bbox[1] for unit in parsed.units)
    assert parsed.units[0].regions[0].bbox[0] == 30


def test_continuing_paragraph_keeps_both_physical_page_regions(tmp_path: Path) -> None:
    """只有页尾未结束、下一页从顶部继续的同章节正文才合并，并保留两页位置。"""
    writer = PdfWriter()
    for commands in ([
        'BT /F1 17 Tf 30 840 Td (Release review) Tj ET',
        'BT /F1 11 Tf 30 80 Td (The release checklist preserves the complete request and its evidence while the review) Tj ET',
    ], [
        'BT /F1 11 Tf 30 840 Td (continues on the next physical page.) Tj ET',
    ]):
        writer.append(PdfReader(BytesIO(positioned_pdf(commands))))
    path = tmp_path / 'continued.pdf'
    writer.write(path)
    parsed = parse_pdf(path)
    assert parsed.error is None
    paragraphs = [unit for unit in parsed.units if unit.kind == 'paragraph']
    assert len(paragraphs) == 1
    assert 'while the review continues on the next physical page.' in paragraphs[0].content
    assert (paragraphs[0].page, paragraphs[0].end_page) == (1, 2)
    assert [region.page for region in paragraphs[0].regions] == [1, 2]


def test_spaced_bold_subheading_uses_numbered_depth(tmp_path: Path) -> None:
    """正文同字号的加粗编号标题需结合独立间距恢复层级，普通编号列表保持正文。"""
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(positioned_pdf([
        'BT /F1 18 Tf 30 840 Td (Release review) Tj ET',
        'BT /F1 11 Tf 30 800 Td (The deployment must retain its evidence.) Tj ET',
        'BT /F2 11 Tf 30 760 Td (1.1 Safety) Tj ET',
        'BT /F1 11 Tf 30 730 Td (1. Check the evidence.) Tj ET',
        'BT /F1 11 Tf 30 705 Td (2. Confirm the owner.) Tj ET',
    ]))))
    resources = writer.pages[0]['/Resources']
    assert isinstance(resources, DictionaryObject)
    fonts = resources['/Font']
    assert isinstance(fonts, DictionaryObject)
    fonts[NameObject('/F2')] = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica-Bold'),
    })
    path = tmp_path / 'bold.pdf'
    writer.write(path)
    parsed = parse_pdf(path)
    assert parsed.error is None
    assert [(unit.content.strip(), unit.heading_level) for unit in parsed.units if unit.kind == 'heading'] == [
        ('Release review', 1), ('1.1 Safety', 2),
    ]
    assert all(unit.kind == 'paragraph' for unit in parsed.units if unit.content.startswith(('1. ', '2. ')))


def test_centered_caption_does_not_absorb_following_body(tmp_path: Path) -> None:
    """英文居中图注与紧接的左对齐正文必须分开，避免后续绑定图注时误删正文。"""
    path = tmp_path / 'caption.pdf'
    path.write_bytes(positioned_pdf([
        'BT /F1 11 Tf 100 700 Td (Fig 1 Release diagram) Tj ET',
        'BT /F1 11 Tf 30 685 Td (Keep this explanation in the document body.) Tj ET',
    ]))
    parsed = parse_pdf(path)
    assert parsed.error is None
    assert [unit.content.strip() for unit in parsed.units] == [
        'Fig 1 Release diagram', 'Keep this explanation in the document body.',
    ]


def test_layout_engine_failure_keeps_plain_text_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第三方版面引擎异常不能让有效 PDF 整份失败，仍需返回可提取文字并标记缺口。"""
    def unavailable_lines(self: Page, *args: object, **kwargs: object) -> list[dict[str, object]]:
        """模拟第三方 PDF 引擎无法提供坐标行，文字层仍由真实 pypdf 提取。"""
        raise RuntimeError('layout engine unavailable')

    path = tmp_path / 'fallback.pdf'
    path.write_bytes(positioned_pdf(['BT /F1 11 Tf 30 700 Td (Keep readable evidence.) Tj ET']))
    monkeypatch.setattr(Page, 'extract_text_lines', unavailable_lines)
    parsed = parse_pdf(path)
    assert parsed.error is None
    assert 'Keep readable evidence.' in ''.join(unit.content for unit in parsed.units)
    assert parsed.info.coverage == 'partial'
    assert '回退纯文字' in ''.join(parsed.info.missing_units)


def test_sample_upgrade_refreshes_descriptions_when_caption_changes(
    architecture_pdf: Path, database_url: str, tmp_path: Path,
) -> None:
    """真实样本旧图片缓存没有图注时，重建必须重新携带图注描述，不能复用旧输入结果。"""
    from general_agent.infrastructure.settings import EmbeddingSettingsStore, StoredEmbeddingSettings
    from tests.attachments.test_attachments import configure_settings
    from tests.attachments.test_document_images import image_files_response
    from tests.auth.support import authenticated_client
    from tests.knowledge.test_knowledge_base import wait_document

    prompts: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """提供受控文件与嵌入协议，只记录本次视觉模型实际收到的原图和图注输入。"""
        if request.method == 'DELETE':
            return httpx.Response(200)
        if request.url.path == '/files':
            return image_files_response()
        payload = json.loads(request.content)
        if 'input' in payload:
            return httpx.Response(200, json={'model': 'test-embedding', 'data': [
                {'index': index, 'embedding': [1.0, 0.0]} for index, _ in enumerate(payload['input'])]})
        prompt = payload['messages'][0]['content'][0]['text']
        prompts.append(prompt)
        return httpx.Response(200, json={'choices': [{'message': {'content': '根据原图和图注生成的说明。'}}]})

    configure_settings(database_url, tmp_path)
    EmbeddingSettingsStore(tmp_path).write(StoredEmbeddingSettings(base_url='https://embedding.example/v1',
        model='test-embedding', api_key='test-key', dimensions=2, revision='v1'))
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        base = client.post('/api/knowledge-bases', json={'name': 'PDF 升级验收'}).json()['id']
        uploaded = client.post(f'/api/knowledge-bases/{base}/documents',
                               files={'files': ('architecture.pdf', architecture_pdf.read_bytes())}).json()[0]
        assert wait_document(client, base, uploaded['id'])['status'] == 'ready'
        assert len(prompts) == 6
        # 将持久缓存设置成升级前缺少图注的真实旧状态，后续行为仍从公开重试入口验证。
        with psycopg.connect(database_url) as connection:
            connection.execute("UPDATE knowledge_document_images SET metadata=metadata-'caption' WHERE document_id=%s",
                               (uploaded['id'],))
        prompts.clear()
        assert client.post(f'/api/knowledge-bases/{base}/documents/{uploaded["id"]}/retry').status_code == 200
        assert wait_document(client, base, uploaded['id'])['status'] == 'ready'
        assert len(prompts) == 6
        assert all('原文图注：图 ' in prompt for prompt in prompts)
