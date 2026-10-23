"""识别 PDF 表格并保留单元格几何、真实表头及合并关系。"""

from dataclasses import dataclass, field
from statistics import median
import re
from typing import Any

from pdfplumber.page import Page
from pdfplumber.table import Table

from general_agent.attachments.knowledge import DocumentUnit, TableCell

BBox = tuple[float, float, float, float]


@dataclass
class PdfTable:
    """保存表格文字、真实单元格、逐行位置和列边界，不把合并格转换为重复文字。"""

    bbox: BBox
    rows: list[list[str]]
    cells: list[TableCell] = field(default_factory=list)
    row_boxes: list[BBox] = field(default_factory=list)
    columns: list[float] = field(default_factory=list)
    header_rows: int = 1
    title: str | None = None
    title_bbox: BBox | None = None
    preview: bool = False


def table_caption(text: str) -> bool:
    """识别原文表题和续表编号，不把普通含“表”字的正文当作表题。"""
    return re.match(r'^(?:续?表|table)\s*(?:\d+|[一二三四五六七八九十]+)', text.strip(), re.I) is not None


def bind_table_titles(page: Page, tables: list[PdfTable]) -> None:
    """在所有近邻候选中先匹配距离最近的表题，防止上下两张表互相抢占说明。"""
    captions = [line for line in page.extract_text_lines(return_chars=False) if table_caption(line['text'])]
    pairs: list[tuple[float, int, int]] = []
    for index, table in enumerate(tables):
        for caption_index, line in enumerate(captions):
            if min(table.bbox[2], line['x1']) <= max(table.bbox[0], line['x0']):
                continue
            if line['bottom'] <= table.bbox[1]:
                distance = table.bbox[1] - line['bottom']
            elif line['top'] >= table.bbox[3]:
                distance = line['top'] - table.bbox[3] + 4
            else:
                continue
            if distance <= 32:
                pairs.append((distance, index, caption_index))
    used: set[int] = set()
    for _, index, caption_index in sorted(pairs):
        if tables[index].title is not None or caption_index in used:
            continue
        line = captions[caption_index]
        tables[index].title = line['text'].strip()
        tables[index].title_bbox = (line['x0'], line['top'], line['x1'], line['bottom'])
        used.add(caption_index)


def overlapping(left: BBox, right: BBox) -> bool:
    """判断两个候选区域是否实质相交，防止有框和无框策略重复识别同一张表。"""
    return min(left[2], right[2]) > max(left[0], right[0]) and min(left[3], right[3]) > max(left[1], right[1])


def separated_columns(table: Table, words: list[dict[str, Any]]) -> bool:
    """无框候选需有稳定宽列间隙和短单元格，避免把对齐正文误判为表格。"""
    gaps: list[list[float]] = [[] for _ in range(len(table.columns) - 1)]
    populated = 0
    for row in table.rows:
        cells: list[list[dict[str, Any]]] = []
        for cell in row.cells:
            selected = [] if cell is None else [word for word in words if
                cell[0] <= (word['x0'] + word['x1']) / 2 <= cell[2]
                and cell[1] <= (word['top'] + word['bottom']) / 2 <= cell[3]]
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


def numeric_cell(text: str) -> bool:
    """判断单元格是否主要为数值，供保守区分文字表头和数字数据行。"""
    return re.fullmatch(r'[￥¥$€£]?\s*[-+]?\d[\d,.]*(?:\s*[%％]|\s*[a-zA-Z]+)?', text.strip()) is not None


def identify_header(rows: list[list[str]], cells: list[TableCell]) -> int:
    """依据文字到数据的变化和顶部合并格判断表头，数字矩阵不把第一行冒充表头。"""
    if not rows or all(numeric_cell(value) for value in rows[0] if value):
        return 0
    for index, row in enumerate(rows[:6]):
        if any(numeric_cell(value) for value in row if value):
            return index
    if any(cell.row == 0 and cell.colspan > 1 for cell in cells) and len(rows) > 2:
        return 2
    return 1


def table_structure(source: Table, *, ruled: bool) -> PdfTable:
    """保留网格合并关系，无框表格只去掉引擎根据文字间距产生的虚拟空行。"""
    xs = sorted({value for cell in source.cells for value in (cell[0], cell[2])})
    ys = sorted({value for cell in source.cells for value in (cell[1], cell[3])})
    if len(xs) > 257 or len(ys) > 20001 or len(source.cells) > 20000:
        raise ValueError('PDF 表格超过列数或单元格资源限制，请拆分后上传。')
    extracted = source.extract()
    raw_rows = [[cell.strip() if cell else '' for cell in row] for row in extracted]
    active = [index for index, row in enumerate(raw_rows) if ruled or any(row)]
    rows = [raw_rows[index] for index in active]
    cells: list[TableCell] = []
    for index, row in enumerate(source.rows):
        for column, bbox in enumerate(row.cells):
            if bbox is None:
                continue
            first, last = ys.index(bbox[1]), ys.index(bbox[3])
            covered = [number for number in active if first <= number < last]
            if not covered:
                continue
            cells.append(TableCell(row=active.index(covered[0]), column=xs.index(bbox[0]),
                                   rowspan=len(covered),
                                   colspan=xs.index(bbox[2]) - xs.index(bbox[0]),
                                   text=raw_rows[index][column]))
    boxes = [(source.bbox[0], ys[index], source.bbox[2], ys[index + 1]) for index in active]
    title: str | None = None
    title_box: BBox | None = None
    first_cells = [cell for cell in cells if cell.row == 0]
    if len(first_cells) == 1 and first_cells[0].colspan == len(xs) - 1 and first_cells[0].rowspan == 1 and table_caption(first_cells[0].text):
        title, title_box = first_cells[0].text, boxes[0]
        rows, boxes = rows[1:], boxes[1:]
        cells = [cell.model_copy(update={'row': cell.row - 1}) for cell in cells if cell.row > 0]
    headers = identify_header(rows, cells)
    for cell in cells:
        cell.header = cell.row < headers
    return PdfTable(source.bbox, rows, cells, boxes, xs, headers, title, title_box, headers == 0)


def table_position(table: PdfTable) -> tuple[float, float]:
    """按页面从上到下、同高从左到右排列表格。"""
    return table.bbox[1], table.bbox[0]


def page_tables(page: Page) -> list[PdfTable]:
    """先识别线框表格，再保守补充无框表格，并排除空表及重叠候选。"""
    found: list[PdfTable] = []
    words: list[dict[str, Any]] | None = None
    for ruled in (True, False):
        settings = {} if ruled else {'vertical_strategy': 'text', 'horizontal_strategy': 'text', 'min_words_vertical': 3}
        for table in page.find_tables(settings):
            if any(overlapping(table.bbox, item.bbox) for item in found) or len(table.columns) < 2:
                continue
            if not ruled:
                if words is None:
                    words = page.extract_words()
                if not separated_columns(table, words):
                    continue
            parsed = table_structure(table, ruled=ruled)
            if sum(bool(any(row)) for row in parsed.rows) >= 2:
                found.append(parsed)
    bind_table_titles(page, found)
    return sorted(found, key=table_position)


def table_header_signature(group: list[DocumentUnit]) -> tuple[tuple[str, int, int], ...]:
    """用每列表头和跨度形成续表证据，要求下一页声明相同列结构。"""
    headers = [cell for unit in group if unit.table_header for cell in unit.table_cells]
    return tuple(sorted((cell.text.strip(), cell.column, cell.colspan) for cell in headers))


def annotate_cross_page_tables(units: list[DocumentUnit], page_sizes: dict[int, tuple[float, float]]) -> None:
    """仅在表格抵近页尾、下一页从顶部重复相同表头时合并身份，独立相邻表仍分开。"""
    groups: dict[int, list[DocumentUnit]] = {}
    for unit in units:
        if unit.table is not None:
            groups.setdefault(unit.table, []).append(unit)
    ordered = sorted(groups.values(), key=lambda group: (group[0].page or 1, group[0].paragraph))
    for previous, following in zip(ordered, ordered[1:]):
        if not previous or not following:
            continue
        previous_page, next_page = previous[-1].page or 1, following[0].page or 1
        previous_regions, next_regions = previous[-1].regions, following[0].regions
        if previous_page + 1 != next_page or not previous_regions or not next_regions:
            continue
        previous_height = page_sizes[previous_page][1]
        next_height = page_sizes[next_page][1]
        same_header = table_header_signature(previous) == table_header_signature(following)
        if (same_header and previous_regions[-1].bbox[3] >= previous_height * .82
                and next_regions[0].bbox[1] <= next_height * .18):
            base = previous[0].table
            if base is None:
                continue
            previous_rows = [cell.row for unit in previous if not unit.table_header for cell in unit.table_cells]
            next_rows = [cell.row for unit in following if not unit.table_header for cell in unit.table_cells]
            row_offset = max(previous_rows, default=-1) + 1 - min(next_rows, default=0)
            for index, unit in enumerate(following):
                unit.table = base
                unit.table_continued = index == 0
                unit.row = (unit.row or 1) + row_offset
                unit.table_cells = [cell.model_copy(update={'row': cell.row + row_offset}) for cell in unit.table_cells]
                if unit.table_header:
                    unit.content = ''
            following[0].table_title = previous[0].table_title
