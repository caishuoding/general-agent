"""通过真实 PDF 内容流验证完整图区域、图注配对和原文媒体顺序。"""

from pathlib import Path
import json
from uuid import UUID

import httpx

from general_agent.attachments.documents import parse_pdf
from tests.attachments.test_pdf_tables import positioned_pdf


def vector_diagram_pdf() -> bytes:
    """绘制包含矩形、箭头和独立文字的流程图，确保原文件完全没有内嵌栅格图。"""
    return positioned_pdf([
        'BT /F1 11 Tf 30 730 Td (BEFORE DIAGRAM) Tj ET',
        '0.9 0.95 1 rg 30 600 150 50 re B',
        '0.9 0.95 1 rg 250 600 150 50 re B',
        '0 0 0 RG 180 625 m 250 625 l S',
        '245 630 m 250 625 l 245 620 l S',
        '0 0 0 rg BT /F1 11 Tf 45 620 Td (Order) Tj ET',
        'BT /F1 11 Tf 265 620 Td (Paid) Tj ET',
        'BT /F1 11 Tf 30 580 Td (Fig 1 Order flow) Tj ET',
        'BT /F1 11 Tf 30 520 Td (AFTER DIAGRAM) Tj ET',
    ])


def test_vector_figure_is_cropped_with_labels_and_bound_caption(tmp_path: Path) -> None:
    """页面上的矢量图应成为完整原图，图内标签和图注不能重复进入普通正文。"""
    path = tmp_path / 'vector.pdf'
    path.write_bytes(vector_diagram_pdf())
    directory = tmp_path / 'images'
    parsed = parse_pdf(path, directory)
    assert parsed.error is None
    assert len(parsed.images) == 1
    image = parsed.images[0]
    assert image.caption == 'Fig 1 Order flow'
    assert image.bbox is not None
    assert image.bbox[0] <= 30 and image.bbox[2] >= 400
    assert image.width >= 700 and image.height >= 95
    assert (directory / image.storage_name).is_file()
    body = ''.join(unit.content for unit in parsed.units)
    assert 'BEFORE DIAGRAM' in body and 'AFTER DIAGRAM' in body
    assert 'Order' not in body and 'Paid' not in body


def test_two_figures_bind_nearest_caption_in_visual_order(tmp_path: Path) -> None:
    """同页左右两图即使绘制指令顺序相反，也应按视觉位置编号并匹配各自图注。"""
    commands = ['BT /F1 11 Tf 30 760 Td (TWO FIGURES) Tj ET']
    for x, name in ((280, 'Right'), (30, 'Left')):
        commands.extend([
            f'{x} 620 170 60 re S',
            f'{x + 20} 630 m {x + 140} 660 l S',
            f'{x + 20} 650 m {x + 140} 640 l S',
            f'BT /F1 11 Tf {x + 30} 645 Td ({name} chart) Tj ET',
        ])
    commands.extend([
        'BT /F1 11 Tf 30 600 Td (Fig 1 Left chart) Tj ET',
        'BT /F1 11 Tf 280 600 Td (Fig 2 Right chart) Tj ET',
    ])
    path = tmp_path / 'two.pdf'
    path.write_bytes(positioned_pdf(commands))
    parsed = parse_pdf(path, tmp_path / 'images')
    assert parsed.error is None
    assert [(image.ordinal, image.caption) for image in parsed.images] == [
        (1, 'Fig 1 Left chart'), (2, 'Fig 2 Right chart'),
    ]
    assert parsed.images[0].bbox is not None and parsed.images[1].bbox is not None
    assert parsed.images[0].bbox[0] < parsed.images[1].bbox[0]


def test_uploaded_pdf_reads_body_image_body_in_original_order(database_url: str, tmp_path: Path) -> None:
    """真实上传后阅读切片必须按正文、图片、正文排列，正文片不能跨过图片位置。"""
    from general_agent.attachments.service import AttachmentService
    from general_agent.attachments.tools import DocumentTools
    from general_agent.infrastructure.database import Database
    from general_agent.infrastructure.tokens import TokenCounter
    from tests.attachments.test_attachments import configure, configure_settings
    from tests.attachments.test_document_images import image_files_response, wait_ready
    from tests.attachments.test_knowledge import owner
    from tests.auth.support import authenticated_client

    def provider(request: httpx.Request) -> httpx.Response:
        """只在视觉输入边界返回受控图片说明，文件操作沿用正式协议。"""
        if request.url.path == '/files':
            return image_files_response()
        if request.method == 'DELETE':
            return httpx.Response(200)
        payload = json.loads(request.content)
        assert 'Fig 1 Order flow' in payload['messages'][0]['content'][0]['text']
        return httpx.Response(200, json={'choices': [{'message': {'content': 'FLOW IMAGE DESCRIPTION'}}]})

    configure_settings(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',
                               files={'files': ('flow.pdf', vector_diagram_pdf())}).json()[0]
        assert wait_ready(client, conversation, uploaded['id'])['status'] == 'ready'
        db = Database(database_url)
        attachments = AttachmentService(db, tmp_path)
        tools = DocumentTools(db, attachments.policy)
        result = tools.read(UUID(conversation), owner(database_url, conversation),
                            {'document_id': uploaded['id'], 'start_page': 1, 'end_page': 1}, TokenCounter('test'), 10000)
        assert [chunk['content'].strip() for chunk in result['content']] == [
            'BEFORE DIAGRAM', 'FLOW IMAGE DESCRIPTION', 'AFTER DIAGRAM',
        ]
        assert result['content'][1]['image_id']
