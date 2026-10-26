"""把整页扫描图的外部版面 OCR 结果转换为现有正文和图片中间结构。"""

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from time import monotonic
import os
import re

import httpx
from PIL import Image
from pdfplumber.page import Page
from pydantic import BaseModel, Field, FiniteFloat

from general_agent.attachments.document_images import MAX_DOCUMENT_IMAGES, MAX_IMAGE_PIXELS, ParsedImage, save_image
from general_agent.attachments.ocr_tables import table_units
from general_agent.attachments.pdf_media import caption_distance, contains_center
from general_agent.attachments.knowledge import DocumentRegion, DocumentUnit

if TYPE_CHECKING:
    from general_agent.attachments.documents import ParsedDocument

OCR_REVISION = 'pp-structure-v3-3.3.2-v1'
MAX_OCR_RESPONSE = 16_000_000


class OcrBlock(BaseModel):
    """校验独立 OCR 服务的区域、文字与可选置信度，拒绝无穷坐标和超长正文。"""

    label: str = Field(max_length=80)
    bbox: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]
    content: str = Field(default='', max_length=2_000_000)
    confidence: float | None = Field(default=None, ge=0, le=1)


class OcrPage(BaseModel):
    """规定 OCR 坐标系为整页可逆旋转后的像素空间，块列表顺序即阅读顺序。"""

    revision: Literal['pp-structure-v3-3.3.2-v1']
    angle: Literal[0, 90, 180, 270] = 0
    width: int = Field(gt=0, le=20000)
    height: int = Field(gt=0, le=20000)
    blocks: list[OcrBlock] = Field(max_length=20000)
    confidence: float | None = Field(default=None, ge=0, le=1)


def is_scan_page(page: Page, text: str) -> bool:
    """仅把缺少正文且被栅格图覆盖的页面交给 OCR，普通插图和文字页沿用原解析。"""
    if len(''.join(text.split())) > 80 or not page.images:
        return False
    # 横向扫描条带也可覆盖整页；按各水平带的区间并集计算，避免重复图层夸大覆盖。
    boxes = [(max(0., item['x0'] - page.bbox[0]), max(0., item['top'] - page.bbox[1]),
              min(page.width, item['x1'] - page.bbox[0]), min(page.height, item['bottom'] - page.bbox[1]))
             for item in page.images]
    edges = sorted({value for box in boxes for value in (box[1], box[3])})
    covered = 0.
    for top, bottom in zip(edges, edges[1:]):
        intervals = sorted((box[0], box[2]) for box in boxes if box[1] <= top and box[3] >= bottom)
        right, width = 0., 0.
        for left, end in intervals:
            width += max(0., end - max(left, right))
            right = max(right, end)
        covered += width * (bottom - top)
    return covered >= page.width * page.height * .7


def render_scan_page(path: Path, directory: Path, number: int) -> 'ParsedDocument':
    """在受限工作进程中仅渲染选定扫描页，整页 PNG 是短期 OCR 输入而非插图。"""
    import pdfplumber
    from general_agent.attachments.documents import DocumentInfo, ParsedDocument

    with pdfplumber.open(path, pages=[number]) as pdf:
        page = pdf.pages[0]
        if page.width * page.height * 4 > MAX_IMAGE_PIXELS:
            raise ValueError('扫描页渲染超过像素资源限制。')
        rendered = page.to_image(resolution=144, antialias=True, force_mediabox=True)
        try:
            output = BytesIO()
            rendered.original.save(output, 'PNG')
            image = save_image(output.getvalue(), directory, 1, page=number, paragraph=1,
                               section=[], location=f'第 {number} 页扫描原文')
            image.bbox = (float(page.bbox[0]), float(page.bbox[1]), float(page.bbox[2]), float(page.bbox[3]))
        finally:
            rendered.original.close()
            rendered.annotated.close()
        return ParsedDocument(info=DocumentInfo(format='pdf'), images=[image])


def recognize_page(path: Path, transport: httpx.BaseTransport | None, timeout: float = 300) -> OcrPage:
    """有界调用私网 OCR 服务，只传 PNG 且不透传模型凭据或服务端原始错误。"""
    endpoint = os.environ.get('DOCUMENT_OCR_URL', '').strip().rstrip('/')
    if not endpoint:
        raise ValueError('未配置扫描 PDF OCR 服务（DOCUMENT_OCR_URL）。')
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream('POST', endpoint + '/parse', content=path.read_bytes(),
                               headers={'Content-Type': 'image/png'}) as response:
                response.raise_for_status()
                output = bytearray()
                for chunk in response.iter_bytes():
                    output.extend(chunk)
                    if len(output) > MAX_OCR_RESPONSE:
                        raise ValueError('OCR 响应超过输出资源限制。')
        return OcrPage.model_validate_json(output)
    except httpx.HTTPError:
        raise ValueError('扫描 PDF OCR 服务不可用或识别超时。') from None
    except ValueError as error:
        if '资源限制' in str(error):
            raise
        raise ValueError('OCR 返回的版面结构或规则版本无效。') from None


def original_bbox(block: OcrBlock, result: OcrPage, image: ParsedImage) -> tuple[float, float, float, float]:
    """把逆时针整页旋转后的像素框逆映射到原始 PDF 的 point 坐标。"""
    left, top, right, bottom = block.bbox
    if not (0 <= left < right <= result.width and 0 <= top < bottom <= result.height):
        raise ValueError('OCR 返回的区域坐标超出原页。')
    if result.angle == 90:
        left, top, right, bottom = image.width - bottom, left, image.width - top, right
    elif result.angle == 180:
        left, top, right, bottom = image.width - right, image.height - bottom, image.width - left, image.height - top
    elif result.angle == 270:
        left, top, right, bottom = top, image.height - right, bottom, image.height - left
    assert image.bbox is not None
    x0, y0, x1, y1 = image.bbox
    return (round(x0 + left / image.width * (x1 - x0), 3), round(y0 + top / image.height * (y1 - y0), 3),
            round(x0 + right / image.width * (x1 - x0), 3), round(y0 + bottom / image.height * (y1 - y0), 3))


def crop_block(source: Image.Image, block: OcrBlock, result: OcrPage, original: ParsedImage,
               directory: Path, ordinal: int, order: int, section: list[str]) -> ParsedImage:
    """先检查图片额度再裁剪相同旋转画布，定位保存逆变换后的 PDF 区域。"""
    if ordinal > MAX_DOCUMENT_IMAGES:
        raise ValueError('文档插图及表格截图超过 50 张资源限制。')
    cropped = source.crop(block.bbox)
    try:
        output = BytesIO()
        cropped.save(output, 'PNG')
        image = save_image(output.getvalue(), directory, ordinal, page=original.page, paragraph=order,
                           section=list(section), location=f'第 {original.page} 页插图 {ordinal}')
    finally:
        cropped.close()
    image.bbox = original_bbox(block, result, original)
    image.reading_order = order
    return image


def whole_page_figure(block: OcrBlock, result: OcrPage) -> bool:
    """拒绝几乎覆盖整页的图片区误判，防止再次把扫描原页送给视觉模型。"""
    left, top, right, bottom = block.bbox
    return (block.label in ('image', 'chart')
            and (right - left) * (bottom - top) >= result.width * result.height * .9)


def page_content(result: OcrPage, image: ParsedImage, directory: Path, image_offset: int,
                 table_offset: int) -> tuple[list[DocumentUnit], list[ParsedImage], list[str]]:
    """把有序版面转成正文、表格及独立插图，已归属图片和表格的文字只保留一份。"""
    expected_size = (image.height, image.width) if result.angle % 180 else (image.width, image.height)
    if (result.width, result.height) != expected_size:
        raise ValueError('OCR 坐标系与原页不一致。')
    for block in result.blocks:
        original_bbox(block, result, image)
    units: list[DocumentUnit] = []
    images: list[ParsedImage] = []
    notes: list[str] = []
    section: list[str] = []
    media = [block for block in result.blocks if block.label in ('image', 'chart', 'table')
             and not whole_page_figure(block, result)]
    captions = [block for block in result.blocks if block.label in ('figure_title', 'figure_table_title')]
    caption_orders = [order for order, block in enumerate(result.blocks, 1)
                      if block.label in ('figure_title', 'figure_table_title')]
    used_captions: set[int] = set()
    with Image.open(directory / image.storage_name) as original:
        source = original.rotate(result.angle, expand=True)
    try:
        for order, block in enumerate(result.blocks, 1):
            bbox = original_bbox(block, result, image)
            if whole_page_figure(block, result):
                notes.append(f'第 {image.page} 页 OCR 将整页识别为插图，已保留正文并跳过整页视觉描述')
                continue
            if block.label in ('image', 'chart'):
                picture = crop_block(source, block, result, image, directory,
                                     image_offset + len(images) + 1, order, section)
                assert image.bbox is not None
                max_gap = 48 * image.width / max(1, image.bbox[2] - image.bbox[0])
                matches = [(distance, index) for index, caption in enumerate(captions)
                           if index not in used_captions
                           and (distance := caption_distance(block.bbox, caption.bbox, max_gap)) is not None]
                if matches:
                    _, index = min(matches)
                    picture.caption = captions[index].content.strip()
                    used_captions.add(index)
                images.append(picture)
                continue
            if block.label == 'table':
                table_offset += 1
                try:
                    rows = table_units(block.content, image.page or 1, bbox, table_offset, order, section)
                except ValueError as error:
                    if '资源限制' in str(error):
                        raise
                    picture = crop_block(source, block, result, image, directory,
                                         image_offset + len(images) + 1, order, section)
                    picture.media_type = 'table_preview'
                    picture.table_ordinal = table_offset
                    images.append(picture)
                    # 无法确认结构时保留可读纯文字，并将表格区域截图绑定到同一表格切片。
                    from general_agent.attachments.ocr_tables import table_plain_text
                    text = table_plain_text(block.content) or '[表格结构未识别，请核对原区域截图]'
                    rows = [DocumentUnit(page=image.page, end_page=image.page, paragraph=order,
                                         content=text + '\n\n', kind='table_row', table=table_offset, row=1,
                                         table_image_id=picture.id, table_columns=1,
                                         section=list(section), reading_order=order, text_source='ocr', ocr_label='table_preview',
                                         regions=[DocumentRegion(page=image.page or 1, bbox=bbox)],
                                         location=f'第 {image.page} 页 OCR 表格 {table_offset}')]
                    notes.append(f'第 {image.page} 页表格 {table_offset} 结构不完整，已保留原区域截图')
                units.extend(rows)
                continue
            if not block.content.strip():
                continue
            if block.label == 'page_number' and re.fullmatch(r'(?:第\s*)?\d+(?:\s*页)?', block.content.strip()):
                continue
            if any(contains_center(item.bbox, block.bbox) for item in media):
                continue
            heading = block.label in ('doc_title', 'paragraph_title')
            if heading:
                section = [block.content.strip()] if block.label == 'doc_title' else section[:1] + [block.content.strip()]
            units.append(DocumentUnit(
                page=image.page, end_page=image.page, paragraph=order, content=block.content.strip() + '\n\n',
                kind='heading' if heading else 'paragraph', heading_level=1 if block.label == 'doc_title' else 2 if heading else None,
                section=list(section), location=f'第 {image.page} 页 OCR 正文',
                regions=[DocumentRegion(page=image.page or 1, bbox=bbox)],
                reading_order=order, text_source='ocr', confidence=block.confidence,
                ocr_label=block.label,
            ))
        bound = {caption_orders[index] for index in used_captions}
        units = [unit for unit in units if unit.reading_order not in bound]
        return units, images, notes
    except Exception:
        for picture in images:
            (directory / picture.storage_name).unlink(missing_ok=True)
        raise
    finally:
        source.close()


def complete_scans(path: Path, directory: Path, document: 'ParsedDocument',
                   transport: httpx.BaseTransport | None, progress: Callable[[int, int], None] | None) -> None:
    """逐页渲染和识别后合入整份中间结果，失败页保留缺口并始终回收整页临时图。"""
    from general_agent.attachments.documents import parse_document_worker, MAX_TEXT_CHARACTERS
    from general_agent.attachments.knowledge import MAX_DOCUMENT_UNITS

    info = document.info
    info.ocr_rules = OCR_REVISION
    rotated_pages: set[int] = set()
    deadline = monotonic() + 3600
    if progress:
        progress(0, len(document.scan_pages))
    for completed, number in enumerate(document.scan_pages, 1):
        image: ParsedImage | None = None
        pictures: list[ParsedImage] = []
        try:
            if monotonic() >= deadline:
                raise ValueError('整份扫描 PDF OCR 超过 60 分钟资源限制。')
            rendered = parse_document_worker(path, f'.scan-page:{number}', directory)
            if rendered.error or not rendered.images:
                raise ValueError(rendered.error or '扫描页渲染未完成。')
            image = rendered.images[0]
            result = recognize_page(directory / image.storage_name, transport, max(1, min(300, deadline - monotonic())))
            units, pictures, notes = page_content(
                result, image, directory, len(document.images),
                max((unit.table or 0 for unit in document.units), default=0),
            )
            if not any(unit.content.strip() for unit in units) and not pictures:
                raise ValueError('OCR 没有识别到可用内容。')
            if len(document.units) + len(units) > MAX_DOCUMENT_UNITS:
                raise ValueError('OCR 内容超过 20,000 个结构块资源限制。')
            if sum(len(unit.content) for unit in document.units + units) > MAX_TEXT_CHARACTERS:
                raise ValueError('OCR 文字超过 2,000,000 字符资源限制。')
            document.units.extend(units)
            document.images.extend(pictures)
            if result.angle:
                rotated_pages.add(number)
            info.missing_units.extend(notes)
            scores = [block.confidence for block in result.blocks if block.confidence is not None]
            confidence = result.confidence if result.confidence is not None else min(scores, default=1)
            if confidence < .8:
                info.ocr_low_confidence_pages.append(number)
                info.missing_units.append(f'第 {number} 页 OCR 置信度较低，请核对原文')
            info.ocr_completed_pages.append(number)
        except (ValueError, OSError) as error:
            for picture in pictures:
                (directory / picture.storage_name).unlink(missing_ok=True)
            info.ocr_failed_pages.append(number)
            info.missing_units.append(f'第 {number} 页 OCR 未完成：{error}')
        finally:
            if image:
                (directory / image.storage_name).unlink(missing_ok=True)
        if progress:
            progress(completed, len(document.scan_pages))
    document.units.sort(key=ocr_unit_order)
    edges: dict[tuple[str, str], set[int]] = {}
    for unit in document.units:
        if unit.ocr_label in ('header', 'footer'):
            key = (unit.ocr_label, re.sub(r'\d+', '#', unit.content.strip()))
            edges.setdefault(key, set()).add(unit.page or 1)
    document.units = [unit for unit in document.units if unit.ocr_label not in ('header', 'footer')
                      or len(edges[(unit.ocr_label, re.sub(r'\d+', '#', unit.content.strip()))]) < 2]
    # 按物理页传播扫描页章节，不跨证据不足的页面猜测合并正文。
    section: list[str] = []
    for unit in document.units:
        if unit.text_source == 'ocr':
            if unit.kind == 'heading':
                section = section[:max(0, (unit.heading_level or 1) - 1)] + [unit.content.strip()]
            unit.section = list(section)
        else:
            section = list(unit.section)
    for picture in document.images:
        if picture.reading_order is not None:
            before = [unit for unit in document.units if ocr_unit_order(unit) <= (picture.page or 1, float(picture.reading_order))]
            if before:
                picture.section = list(before[-1].section)
    from general_agent.attachments.pdf_tables import annotate_cross_page_tables
    # 原页坐标在旋转后不代表阅读方向的页首/页尾，暂不据此合并旋转页的表格。
    mergeable = [unit for unit in document.units if unit.page not in rotated_pages
                 and unit.table_image_id is None]
    annotate_cross_page_tables(mergeable, document.page_sizes)
    info.readable_units = len({page for unit in document.units if unit.content.strip()
                               for page in range(unit.page or 1, (unit.end_page or unit.page or 1) + 1)})
    info.image_count = len(document.images)
    info.coverage = 'partial' if info.missing_units else 'complete'
    if not info.readable_units and not document.images:
        document.error = '扫描 PDF OCR 未能提取可用内容。' + ' '.join(info.missing_units[:1])


def ocr_unit_order(unit: DocumentUnit) -> tuple[int, float]:
    """合并文字页和扫描页时按物理页及已识别阅读顺序排列中间内容。"""
    return unit.page or 1, float(unit.reading_order or (unit.regions[0].bbox[1] if unit.regions else unit.paragraph))
