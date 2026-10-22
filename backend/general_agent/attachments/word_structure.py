"""从 Word 原生段落和表格读取正文，并为独立分页副本添加无可见内容的定位书签。"""

from copy import deepcopy
from html import escape
from docx.document import Document as WordDocument
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.text.paragraph import CT_P
from docx.table import _Cell
from docx.text.paragraph import Paragraph

from general_agent.attachments.knowledge import DocumentUnit, MAX_DOCUMENT_UNITS


def heading_level(paragraph: Paragraph) -> int | None:
    """读取直接格式和继承样式的标题级别，不依赖标题文字是否带编号或井号。"""
    properties = paragraph._p.pPr
    if properties is not None:
        outline = properties.find(qn('w:outlineLvl'))
        if outline is not None:
            value = int(outline.get(qn('w:val'), '9'))
            return value + 1 if value < 9 else None
    style = paragraph.style
    for _ in range(10):
        if style is None:
            break
        properties = style.element.find(qn('w:pPr'))
        outline = properties.find(qn('w:outlineLvl')) if properties is not None else None
        if outline is not None:
            value = int(outline.get(qn('w:val'), '9'))
            return value + 1 if value < 9 else None
        if style.style_id == 'Title':
            return 1
        style = style.base_style
    return None


def cell_text(cell: _Cell, depth: int = 0) -> str:
    """按原文顺序保留单元格段落及嵌套表格，限制嵌套深度并去重横向合并单元格。"""
    if depth > 8:
        raise ValueError('DOCX 表格嵌套过深，请简化文档后上传。')
    parts: list[str] = []
    for item in cell.iter_inner_content():
        if isinstance(item, Paragraph):
            parts.append(item.text)
        else:
            for row in item.rows:
                seen: set[object] = set()
                values: list[str] = []
                for child in row.cells:
                    if child._tc not in seen:
                        seen.add(child._tc)
                        values.append(cell_text(child, depth + 1))
                parts.append(' | '.join(values))
    return '\n'.join(parts)


def markdown_cell(text: str) -> str:
    """转义单元格管道和 HTML，并用换行标记保留段内换行，避免破坏 Markdown 列结构。"""
    return escape(text, quote=False).replace('\\', '\\\\').replace('|', '\\|').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '<br>')


def add_bookmark(paragraph: CT_P, name: str, number: int, at_end: bool) -> None:
    """在临时副本标记块边界；末端覆盖最后一字，避免超链接段末零宽书签被丢弃。"""
    start, end = OxmlElement('w:bookmarkStart'), OxmlElement('w:bookmarkEnd')
    start.set(qn('w:id'), str(number))
    start.set(qn('w:name'), name)
    end.set(qn('w:id'), str(number))
    if at_end:
        texts = paragraph.xpath('.//w:t')
        if texts and texts[-1].text:
            text = texts[-1]
            run = text.getparent()
            parent = run.getparent()
            last_run = OxmlElement('w:r')
            properties = run.find(qn('w:rPr'))
            if properties is not None:
                last_run.append(deepcopy(properties))
            last_text = deepcopy(text)
            last_text.text = text.text[-1]
            text.text = text.text[:-1]
            last_run.append(last_text)
            # 将末字后的换行等节点一并移到新 run，保持原有可见内容和分页顺序。
            for child in list(run)[list(run).index(text) + 1:]:
                last_run.append(child)
            position = list(parent).index(run) + 1
            parent.insert(position, start)
            parent.insert(position + 1, last_run)
            parent.insert(position + 2, end)
        else:
            paragraph.append(start)
            paragraph.append(end)
    else:
        position = 1 if paragraph.find(qn('w:pPr')) is not None else 0
        paragraph.insert(position, start)
        paragraph.insert(position + 1, end)


def word_units(document: WordDocument, bookmark_prefix: str | None = None) -> list[DocumentUnit]:
    """逐块读取 Word 原文，表格转为标准 Markdown；可在相同块边界添加分页定位书签。"""
    units: list[DocumentUnit] = []
    section: list[tuple[int, str]] = []
    tables, characters = 0, 0
    bookmark_base = max((int(item.get(qn('w:id'), '0')) for item in document.element.xpath('.//w:bookmarkStart')), default=0) + 1
    for item in document.iter_inner_content():
        entries: list[tuple[DocumentUnit, CT_P, CT_P]] = []
        if isinstance(item, Paragraph):
            if not item.text.strip():
                continue
            level = heading_level(item)
            if level is not None:
                section = [(depth, title) for depth, title in section if depth < level]
                section.append((level, item.text))
            unit = DocumentUnit(paragraph=0, content=item.text + '\n\n',
                                kind='heading' if level is not None else 'paragraph', heading_level=level,
                                section=[title for _, title in section], location='正文段落')
            entries.append((unit, item._p, item._p))
        else:
            tables += 1
            for row_number, row in enumerate(item.rows, 1):
                values: list[str] = []
                seen: set[object] = set()
                for column, cell in enumerate(row.cells):
                    if cell._tc in seen:
                        values.append('')
                        continue
                    seen.add(cell._tc)
                    values.append(markdown_cell(cell_text(cell)))
                paragraphs = row._tr.xpath('.//w:p')
                if not paragraphs:
                    continue
                content = '| ' + ' | '.join(values) + ' |\n'
                if row_number == 1:
                    content += '| ' + ' | '.join('---' for _ in values) + ' |\n'
                unit = DocumentUnit(paragraph=0, content=content, kind='table_row',
                                    table=tables, row=row_number, section=[title for _, title in section],
                                    location=f'表格 {tables}，第 {row_number} 行')
                entries.append((unit, paragraphs[0], paragraphs[-1]))
        for unit, first, last in entries:
            number = len(units) + 1
            characters += len(unit.content)
            if number > MAX_DOCUMENT_UNITS or characters > 2_000_000:
                raise ValueError('Word 原文超过 20,000 个结构块或 2,000,000 字符资源限制，请拆分后上传。')
            unit.paragraph = number
            if unit.table is None:
                unit.location = f'正文段落 {number}'
            units.append(unit)
            if bookmark_prefix is not None:
                add_bookmark(first, f'{bookmark_prefix}S{number}', bookmark_base + number * 2, False)
                add_bookmark(last, f'{bookmark_prefix}E{number}', bookmark_base + number * 2 + 1, True)
    return units
