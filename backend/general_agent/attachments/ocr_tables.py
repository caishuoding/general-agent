"""把 OCR 表格 HTML 转成现有单元格与行结构，不执行 HTML 或外部资源。"""

from html.parser import HTMLParser

from general_agent.attachments.knowledge import DocumentRegion, DocumentUnit, TableCell
from general_agent.attachments.word_structure import markdown_cell


class OcrTableParser(HTMLParser):
    """保留行列跨度与显式表头，限制网格大小并拒绝嵌套表格。"""

    def __init__(self) -> None:
        """初始化有限网格、当前单元格及可读文字回退。"""
        super().__init__(convert_charrefs=True)
        self.cells: list[TableCell] = []
        self.row = -1
        self.column = 0
        self.occupied: set[tuple[int, int]] = set()
        self.current: TableCell | None = None
        self.parts: list[str] = []
        self.in_header = False
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """读取表格行和单元格，跨度进入现有 TableCell，过大或重叠网格明确失败。"""
        if tag == 'table':
            self.depth += 1
            if self.depth > 1:
                raise ValueError('OCR 嵌套表格不能可靠恢复。')
        elif tag == 'thead':
            self.in_header = True
        elif tag == 'tr':
            self.row += 1
            self.column = 0
            if self.row >= 20000:
                raise ValueError('OCR 表格超过行数资源限制。')
        elif tag in ('td', 'th'):
            if self.row < 0 or self.current is not None:
                raise ValueError('OCR 表格单元格结构不完整。')
            options = dict(attrs)
            rowspan = int(options.get('rowspan') or '1')
            colspan = int(options.get('colspan') or '1')
            while (self.row, self.column) in self.occupied:
                self.column += 1
            if not (1 <= rowspan <= 20000 and 1 <= colspan <= 256 and self.column + colspan <= 256):
                raise ValueError('OCR 表格跨度超过资源限制。')
            if len(self.occupied) + rowspan * colspan > 20000:
                raise ValueError('OCR 表格超过单元格资源限制。')
            for row in range(self.row, self.row + rowspan):
                for column in range(self.column, self.column + colspan):
                    if (row, column) in self.occupied:
                        raise ValueError('OCR 表格单元格重叠。')
                    self.occupied.add((row, column))
            self.current = TableCell(row=self.row, column=self.column, text='', rowspan=rowspan,
                                     colspan=colspan, header=tag == 'th' or self.in_header)
            self.parts = []
            self.column += colspan
        elif tag == 'br' and self.current is not None:
            self.parts.append('\n')

    def handle_data(self, data: str) -> None:
        """仅收集单元格纯文字，HTML 标签不会进入展示或索引。"""
        if self.current is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        """结束单元格与表头分区，保留每格原文和真实跨度。"""
        if tag in ('td', 'th') and self.current is not None:
            self.current.text = ''.join(self.parts).strip()
            self.cells.append(self.current)
            self.current = None
        elif tag == 'thead':
            self.in_header = False
        elif tag == 'table':
            self.depth -= 1


def table_units(html: str, page: int, bbox: tuple[float, float, float, float],
                ordinal: int, order: int, section: list[str]) -> list[DocumentUnit]:
    """将显式表头和跨度转成现有 Markdown 行，无法确定表头时由调用方截图兜底。"""
    parser = OcrTableParser()
    parser.feed(html)
    parser.close()
    cells = parser.cells
    if parser.current is not None or parser.depth != 0 or not cells or not any(cell.header for cell in cells):
        raise ValueError('OCR 表格没有可靠的显式表头。')
    columns = max(cell.column + cell.colspan for cell in cells)
    rows = max(cell.row + cell.rowspan for cell in cells)
    units: list[DocumentUnit] = []
    for row in range(rows):
        selected = [cell for cell in cells if cell.row == row]
        values = [''] * columns
        for cell in selected:
            values[cell.column] = markdown_cell(cell.text)
        text = '| ' + ' | '.join(values) + ' |\n'
        if row == 0:
            text += '| ' + ' | '.join('---' for _ in values) + ' |\n'
        units.append(DocumentUnit(page=page, end_page=page, paragraph=order, content=text, kind='table_row',
                                  table=ordinal, row=row + 1, table_columns=columns, table_cells=selected,
                                  table_header=bool(selected) and all(cell.header for cell in selected),
                                  section=list(section), reading_order=order, text_source='ocr',
                                  regions=[DocumentRegion(page=page, bbox=bbox)],
                                  location=f'第 {page} 页 OCR 表格 {ordinal}，第 {row + 1} 行'))
    return units


class PlainTableParser(HTMLParser):
    """结构不可靠的 OCR 表格只保留纯文字，标签和属性不参与展示。"""

    def __init__(self) -> None:
        """初始化有序纯文字列表。"""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        """按出现顺序收集 OCR 表格可读内容。"""
        if data.strip():
            self.parts.append(data.strip())


def table_plain_text(html: str) -> str:
    """为截图兜底提取可检索的表格文字，不猜测缺失的单元格跨度。"""
    parser = PlainTableParser()
    parser.feed(html)
    parser.close()
    return ' | '.join(parser.parts)
