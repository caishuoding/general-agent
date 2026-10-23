"""从文字型 PDF 的坐标和线条恢复表格，表格与普通正文按位置交错输出。"""

from dataclasses import dataclass
from statistics import median
from typing import Any

from pdfplumber.page import Page
from pdfplumber.table import Table

from general_agent.attachments.knowledge import DocumentRegion, DocumentUnit
from general_agent.attachments.pdf_text import PdfLine, PdfTextLayout, margin_line, paragraph_units, text_line
from general_agent.attachments.word_structure import markdown_cell


@dataclass
class PdfTable:
    """保存经校验的表格区域及单元格，空值对应合并格或空白格，不重复填充数据。"""

    bbox: tuple[float, float, float, float]
    rows: list[list[str]]


def overlapping(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    """判断两个区域是否实质相交，防止有框与无框策略对同一表格重复识别。"""
    return min(left[2], right[2]) > max(left[0], right[0]) and min(left[3], right[3]) > max(left[1], right[1])


def separated_columns(table: Table, words: list[dict[str, Any]]) -> bool:
    """无框候选需有稳定宽列间隙和短单元格，避免将连续正文或双栏文章当成表格。"""
    gaps: list[list[float]] = [[] for _ in range(len(table.columns) - 1)]
    populated = 0
    for row in table.rows:
        cells: list[list[dict[str, Any]]] = []
        for cell in row.cells:
            selected = [] if cell is None else [word for word in words if
                cell[0] <= (word['x0'] + word['x1']) / 2 <= cell[2] and
                cell[1] <= (word['top'] + word['bottom']) / 2 <= cell[3]]
            cells.append(selected)
        if not any(cells):
            continue
        populated += 1
        if any(len(cell) > 6 or sum(len(word['text']) for word in cell) > 60 for cell in cells):
            return False
        for index, (left, right) in enumerate(zip(cells, cells[1:])):
            if left and right:
                gaps[index].append(min(word['x0'] for word in right) - max(word['x1'] for word in left))
    return populated >= 3 and bool(gaps) and all(len(gap) >= 3 and median(gap) >= 12 for gap in gaps)


def page_tables(page: Page) -> list[PdfTable]:
    """先识别线框表格，再保守补充文字对齐的无框表格，拒绝空表和重叠候选。"""
    found: list[PdfTable] = []
    words: list[dict[str, Any]] | None = None
    for ruled in (True, False):
        settings = {} if ruled else {'vertical_strategy': 'text', 'horizontal_strategy': 'text', 'min_words_vertical': 3}
        for table in page.find_tables(settings):
            if any(overlapping(table.bbox, item.bbox) for item in found):
                continue
            if len(table.columns) < 2:
                continue
            if not ruled:
                if words is None:
                    words = page.extract_words()
                if not separated_columns(table, words):
                    continue
            rows = [[cell.strip() if cell else '' for cell in row] for row in table.extract()]
            rows = [row for row in rows if any(row)]
            if len(rows) >= 2:
                found.append(PdfTable(table.bbox, rows))
    return sorted(found, key=table_position)


def table_position(table: PdfTable) -> tuple[float, float]:
    """按页面从上到下、同高从左到右排列独立表格。"""
    return table.bbox[1], table.bbox[0]


def event_position(event: tuple[float, float, PdfLine | PdfTable]) -> tuple[float, float]:
    """按位置合并正文行和表格，保持表格前后正文的页面顺序。"""
    return event[0], event[1]


def page_units(page: Page, number: int, table_offset: int, layout: PdfTextLayout) -> tuple[list[DocumentUnit], int]:
    """按真实坐标交错输出自然段、标题与表格，表格文字不重复进入正文。"""
    tables = page_tables(page)

    def outside_tables(item: dict[str, Any]) -> bool:
        """从普通正文中排除已归属表格的字符，表格区域以字符中心判断。"""
        x, y = (item['x0'] + item['x1']) / 2, (item['top'] + item['bottom']) / 2
        return not any(table.bbox[0] <= x <= table.bbox[2] and table.bbox[1] <= y <= table.bbox[3] for table in tables)

    lines = [text_line(line) for line in page.filter(outside_tables).extract_text_lines()]
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
            units.append(DocumentUnit(page=number, paragraph=len(units) + 1, content=text, kind='table_row',
                                      table=table_number, row=index, end_page=number,
                                      section=[title for _, title in layout.section],
                                      regions=[DocumentRegion(page=number, bbox=content.bbox)],
                                      location=f'第 {number} 页，表格 {table_number}，第 {index} 行'))
    flush_paragraph()
    for index, unit in enumerate(units, 1):
        unit.paragraph = index
    return units, len(tables)
