"""从文字型 PDF 的坐标和线条恢复表格，表格与普通正文按位置交错输出。"""

from pathlib import Path
from typing import Any

from pdfplumber.page import Page

from general_agent.attachments.knowledge import DocumentRegion, DocumentUnit
from general_agent.attachments.pdf_text import PdfLine, PdfTextLayout, margin_line, paragraph_units, text_line
from general_agent.attachments.document_images import ParsedImage
from general_agent.attachments.pdf_media import contains_center, object_bbox, page_images, split_caption_line
from general_agent.attachments.word_structure import markdown_cell
from general_agent.attachments.pdf_tables import PdfTable, page_tables


def event_position(event: tuple[float, float, PdfLine | PdfTable]) -> tuple[float, float]:
    """按位置合并正文行和表格，保持表格前后正文的页面顺序。"""
    return event[0], event[1]


def page_units(page: Page, number: int, table_offset: int, layout: PdfTextLayout,
               directory: Path | None = None, image_offset: int = 0) -> tuple[list[DocumentUnit], int, list[ParsedImage]]:
    """按坐标还原正文、表格与完整图片，表格及图内文字不重复进入正文。"""
    tables = page_tables(page)
    images = page_images(page, directory, image_offset, table_offset, tables)
    previews = {image.table_ordinal: image.id for image in images if image.media_type == 'table_preview'}

    def outside_tables(item: dict[str, Any]) -> bool:
        """从普通正文中排除已归属表格的字符，表格区域以字符中心判断。"""
        x, y = (item['x0'] + item['x1']) / 2, (item['top'] + item['bottom']) / 2
        return (not any(table.bbox[0] <= x <= table.bbox[2] and table.bbox[1] <= y <= table.bbox[3] for table in tables)
                and not any(table.title_bbox is not None and contains_center(table.title_bbox, object_bbox(item)) for table in tables)
                and not any(image.bbox is not None and contains_center(image.bbox, object_bbox(item)) for image in images))

    lines = [text_line(line) for value in page.filter(outside_tables).extract_text_lines() for line in split_caption_line(value)]
    lines = [line for line in lines if not margin_line(line, page.height, number, layout)]
    events: list[tuple[float, float, PdfLine | PdfTable]] = [(line.bbox[1], line.bbox[0], line) for line in lines]
    events.extend((table.bbox[1], table.bbox[0], table) for table in tables)
    units: list[DocumentUnit] = []
    paragraphs: list[PdfLine] = []

    def flush_paragraph() -> None:
        """将相邻正文行恢复为带章节和区域的自然段，表格阻断段落聚合。"""
        if paragraphs:
            units.extend(paragraph_units(paragraphs, number, layout))
            paragraphs.clear()

    table_number = table_offset
    for _, _, content in sorted(events, key=event_position):
        if isinstance(content, PdfLine):
            paragraphs.append(content)
            continue
        flush_paragraph()
        table_number += 1
        columns = max(len(row) for row in content.rows)
        for index, row in enumerate(content.rows, 1):
            cells = [markdown_cell(value) for value in row] + [''] * (columns - len(row))
            text = '| ' + ' | '.join(cells) + ' |\n'
            if index == 1:
                text += '| ' + ' | '.join('---' for _ in cells) + ' |\n'
                if content.title:
                    text = content.title + '\n\n' + text
            units.append(DocumentUnit(page=number, paragraph=len(units) + 1, content=text, kind='table_row',
                                      table=table_number, row=index, end_page=number,
                                      section=[title for _, title in layout.section],
                                      table_columns=len(content.columns) - 1, table_header=index <= content.header_rows,
                                      table_cells=[cell for cell in content.cells if cell.row == index - 1],
                                      table_title=content.title, table_image_id=previews.get(table_number),
                                      regions=[DocumentRegion(page=number, bbox=content.row_boxes[index - 1])],
                                      location=f'第 {number} 页，表格 {table_number}，第 {index} 行'))
    flush_paragraph()
    for index, unit in enumerate(units, 1):
        unit.paragraph = index
    for image in images:
        before = [unit for unit in units if image.bbox is not None and unit.regions and unit.regions[0].bbox[1] < image.bbox[1]]
        if before:
            image.section = list(before[-1].section)
            image.paragraph = before[-1].paragraph
    return units, len(tables), images
