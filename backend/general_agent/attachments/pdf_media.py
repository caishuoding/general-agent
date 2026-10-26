"""根据页面版面还原完整图，栅格、矢量线条和图内文字共同裁剪为可展示原图。"""

from dataclasses import dataclass
from io import BytesIO
from math import ceil, floor
from pathlib import Path
from typing import Any

from pdfplumber.page import Page

from general_agent.attachments.document_images import MAX_DOCUMENT_IMAGES, MAX_IMAGE_PIXELS, ParsedImage, figure_caption, save_image
from general_agent.attachments.pdf_tables import PdfTable

BBox = tuple[float, float, float, float]


@dataclass
class FigureRegion:
    """保存聚合后的可见图区域，计数用于排除独立装饰线和普通背景框。"""

    bbox: BBox
    raster: bool = False
    shapes: int = 0


def object_bbox(item: dict[str, Any]) -> BBox:
    """把 PDF 图片和几何对象统一为页面左上角坐标。"""
    return float(item['x0']), float(item['top']), float(item['x1']), float(item['bottom'])


def contains_center(outer: BBox, inner: BBox) -> bool:
    """判断对象中心是否位于已识别区域内，用于移除表格线及图内正文重复。"""
    return outer[0] <= (inner[0] + inner[2]) / 2 <= outer[2] and outer[1] <= (inner[1] + inner[3]) / 2 <= outer[3]


def union_bbox(left: BBox, right: BBox) -> BBox:
    """返回覆盖两个相邻图元的最小外接矩形。"""
    return min(left[0], right[0]), min(left[1], right[1]), max(left[2], right[2]), max(left[3], right[3])


def nearby(left: BBox, right: BBox, gap: float = 8) -> bool:
    """按小范围几何接触连接图元，避免把远离的独立图拼成同一张图。"""
    return min(left[2], right[2]) + gap >= max(left[0], right[0]) and min(left[3], right[3]) + gap >= max(left[1], right[1])


def figure_order(region: FigureRegion) -> tuple[float, float]:
    """按页面从上到下、同高从左到右排列完整图区域。"""
    return region.bbox[1], region.bbox[0]


def figure_regions(page: Page, tables: list[BBox]) -> list[FigureRegion]:
    """聚合内嵌图片及连通矢量图元，排除表格、页边装饰和单独背景框。"""
    regions: list[FigureRegion] = []
    for raster, objects in ((True, page.images), (False, [*page.rects, *page.curves, *page.lines])):
        for item in objects:
            bbox = object_bbox(item)
            width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if any(contains_center(table, bbox) for table in tables):
                continue
            if bbox[3] < page.height * .07 or bbox[1] > page.height * .94:
                continue
            if width > page.width * .92 and height < 3:
                continue
            if width * height > page.width * page.height * .95 and page.chars:
                continue
            if raster and (width < 12 or height < 12):
                continue
            current = FigureRegion(bbox, raster, 0 if raster else 1)
            changed = True
            while changed:
                changed = False
                for existing in list(regions):
                    if nearby(current.bbox, existing.bbox):
                        current.bbox = union_bbox(current.bbox, existing.bbox)
                        current.raster = current.raster or existing.raster
                        current.shapes += existing.shapes
                        regions.remove(existing)
                        changed = True
            regions.append(current)
    return sorted([region for region in regions if region.raster or (
        region.shapes >= 3 and region.bbox[2] - region.bbox[0] >= 60
        and region.bbox[3] - region.bbox[1] >= 30)], key=figure_order)


def caption_distance(region: BBox, caption: BBox, max_gap: float = 48) -> float | None:
    """匹配水平交集且上下邻近的图注；OCR 调用方按渲染比例换算最大间距。"""
    if min(region[2], caption[2]) <= max(region[0], caption[0]):
        return None
    if caption[1] >= region[3]:
        gap = caption[1] - region[3]
    elif caption[3] <= region[1]:
        gap = region[1] - caption[3] + 2
    else:
        return None
    return gap if gap <= max_gap else None


def caption_pair_order(pair: tuple[float, int, int]) -> tuple[float, int, int]:
    """先分配距离最近的图注，确定性地处理同页多图。"""
    return pair


def character_x(character: dict[str, Any]) -> float:
    """按横坐标还原同一视觉行里的字符顺序，不依赖 PDF 绘制指令顺序。"""
    return float(character['x0'])


def split_caption_line(value: dict[str, Any]) -> list[dict[str, Any]]:
    """只拆分同一行中被大空白隔开的多个独立图注，普通正文行保持不变。"""
    groups: list[list[dict[str, Any]]] = [[]]
    for character in sorted(value.get('chars', []), key=character_x):
        if groups[-1] and character['x0'] - groups[-1][-1]['x1'] > max(24, float(character['size']) * 2.5):
            groups.append([])
        groups[-1].append(character)
    if len(groups) < 2:
        return [value]
    result: list[dict[str, Any]] = []
    cursor = 0
    original = str(value['text'])
    for group in groups:
        remaining = len(''.join(''.join(char['text'] for char in group).split()))
        start = cursor
        while cursor < len(original) and remaining:
            if not original[cursor].isspace():
                remaining -= 1
            cursor += 1
        text = original[start:cursor].strip()
        if not figure_caption(text):
            return [value]
        result.append({'text': text, 'chars': group, 'x0': min(char['x0'] for char in group),
                       'x1': max(char['x1'] for char in group), 'top': min(char['top'] for char in group),
                       'bottom': max(char['bottom'] for char in group)})
    return result


def crop_region(rendered: Any, page: Page, bbox: BBox) -> bytes:
    """按页面坐标裁剪渲染图并编码 PNG，供完整图和表格原区域兜底共用。"""
    padded = (max(page.bbox[0], bbox[0] - 2), max(page.bbox[1], bbox[1] - 2),
              min(page.bbox[2], bbox[2] + 2), min(page.bbox[3], bbox[3] + 2))
    pixels = (floor((padded[0] - rendered.bbox[0]) * rendered.scale), floor((padded[1] - rendered.bbox[1]) * rendered.scale),
              ceil((padded[2] - rendered.bbox[0]) * rendered.scale), ceil((padded[3] - rendered.bbox[1]) * rendered.scale))
    cropped = rendered.original.crop(pixels)
    output = BytesIO()
    cropped.save(output, 'PNG')
    cropped.close()
    return output.getvalue()


def page_images(page: Page, directory: Path | None, image_offset: int, table_offset: int,
                tables: list[PdfTable]) -> list[ParsedImage]:
    """一次渲染页面，提取完整图并为无法确定结构的表格保存原区域截图。"""
    regions = figure_regions(page, [table.bbox for table in tables])
    previews = [(index, table) for index, table in enumerate(tables) if table.preview]
    if not regions and not previews:
        return []
    if image_offset + len(regions) + len(previews) > MAX_DOCUMENT_IMAGES:
        raise ValueError('单份 PDF 最多处理 50 张图片，请拆分后上传。')
    if page.width * page.height * 4 > MAX_IMAGE_PIXELS:
        raise ValueError('PDF 页面渲染超过图片像素资源限制，请缩小页面后上传。')
    lines = [line for value in page.extract_text_lines() for line in split_caption_line(value)]
    captions = [(line['text'], object_bbox(line)) for line in lines if figure_caption(line['text'])]
    pairs: list[tuple[float, int, int]] = []
    for index, region in enumerate(regions):
        for caption_index, (_, bbox) in enumerate(captions):
            distance = caption_distance(region.bbox, bbox)
            if distance is not None:
                pairs.append((distance, index, caption_index))
    bound: dict[int, str] = {}
    used: set[int] = set()
    for _, index, caption_index in sorted(pairs, key=caption_pair_order):
        if index not in bound and caption_index not in used:
            bound[index] = captions[caption_index][0]
            used.add(caption_index)
    rendered = page.to_image(resolution=144, antialias=True)
    rendered.annotated.close()
    result: list[ParsedImage] = []
    try:
        for index, region in enumerate(regions):
            bbox = region.bbox
            for line in lines:
                label = object_bbox(line)
                if not figure_caption(line['text']) and contains_center(bbox, label):
                    bbox = union_bbox(bbox, label)
            image = save_image(crop_region(rendered, page, bbox), directory, image_offset + index + 1,
                               page=page.page_number, paragraph=page.page_number, section=[],
                               location=f'第 {page.page_number} 页第 {index + 1} 张图片', caption=bound.get(index))
            image.bbox = (round(bbox[0], 3), round(bbox[1], 3), round(bbox[2], 3), round(bbox[3], 3))
            result.append(image)
        for preview_index, (table_index, table) in enumerate(previews):
            ordinal = image_offset + len(regions) + preview_index + 1
            image = save_image(crop_region(rendered, page, table.bbox), directory, ordinal,
                               page=page.page_number, paragraph=page.page_number, section=[],
                               location=f'第 {page.page_number} 页表格 {table_offset + table_index + 1} 原区域截图',
                               caption=table.title)
            image.bbox = table.bbox
            image.media_type = 'table_preview'
            image.table_ordinal = table_offset + table_index + 1
            result.append(image)
    finally:
        rendered.original.close()
    return result
