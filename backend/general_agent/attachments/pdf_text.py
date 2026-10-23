"""恢复单栏文字型 PDF 的标题和自然段，坐标始终采用页面左上角 point 空间。"""

from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from math import ceil
import re
from typing import Any

from pdfplumber.pdf import PDF
from pypdf import PdfReader

from general_agent.attachments.knowledge import DocumentRegion, DocumentUnit
from general_agent.attachments.document_images import figure_caption

PDF_TEXT_SOURCE = 'pdf_single_column_v4'


@dataclass
class PdfLine:
    """保存正文行文字、字号及坐标，供段落边界判断而不持久化字符级副本。"""

    text: str
    bbox: tuple[float, float, float, float]
    size: float
    bold: bool


@dataclass
class PdfTextLayout:
    """保存全文统计得到的正文字号和标题字号层级；章节状态随页面顺序推进。"""

    body_size: float
    heading_sizes: list[float]
    bookmarks: dict[tuple[int, str], int] = field(default_factory=dict)
    section: list[tuple[int, str]] = field(default_factory=list)
    margins: set[tuple[str, str, int]] = field(default_factory=set)
    page_sizes: dict[int, tuple[float, float]] = field(default_factory=dict)
    incomplete_pages: set[int] = field(default_factory=set)


def normalized_title(text: str) -> str:
    """忽略排版空白比较书签标题，保留原文标点和实际文字。"""
    return re.sub(r'\s+', '', text)


def outline_titles(reader: PdfReader) -> dict[tuple[int, str], int]:
    """读取书签的实际目的页和层级；损坏书签不影响正文解析。"""
    result: dict[tuple[int, str], int] = {}

    def visit(items: list[Any], level: int) -> None:
        """递归展开书签列表，嵌套列表对应上一书签的子章节。"""
        for item in items:
            if isinstance(item, list):
                visit(item, min(level + 1, 6))
            else:
                page = reader.get_destination_page_number(item)
                if page is not None:
                    result[(page + 1, normalized_title(str(item.title)))] = level

    try:
        visit(reader.outline, 1)
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return result


def document_layout(pdf: PDF, reader: PdfReader) -> PdfTextLayout:
    """统计正文及标题字号与跨页重复边缘行，只保留轻量特征并逐页释放字符缓存。"""
    sizes: Counter[float] = Counter()
    all_sizes: set[float] = set()
    page_sizes: dict[int, tuple[float, float]] = {}
    edges: list[tuple[int, tuple[str, str, int], float]] = []
    incomplete: set[int] = set()
    characters = 0
    for page in pdf.pages:
        page_sizes[page.page_number] = (page.width, page.height)
        try:
            characters += len(page.chars)
            if characters > 2_000_000:
                raise ValueError('文档提取文字超过 2,000,000 字符资源限制，请拆分后上传。')
            sizes.update(round(float(char['size']), 1) for char in page.chars
                         if char.get('text', '').strip() and page.height * .07 < char['top'] < page.height * .93)
            all_sizes.update(round(float(char['size']), 1) for char in page.chars if char.get('text', '').strip())
            for value in page.extract_text_lines():
                line = text_line(value)
                signature = margin_signature(line, page.height)
                if signature is not None:
                    edges.append((page.page_number, signature, line.size))
        except MemoryError:
            raise ValueError('文档版面解析超过内存资源限制，请拆分后上传。') from None
        except Exception as error:
            if isinstance(error, ValueError) and '资源限制' in str(error):
                raise
            incomplete.add(page.page_number)
        finally:
            page.close()
    body = sizes.most_common(1)[0][0] if sizes else 12.0
    headings: list[float] = []
    for size in sorted(all_sizes, reverse=True):
        if size > body * 1.07 and (not headings or headings[-1] - size > body * .08):
            headings.append(size)
    bookmarks = outline_titles(reader)
    occurrences: dict[tuple[str, str, int], set[int]] = {}
    for page_number, signature, size in edges:
        if size <= body * 1.07:
            occurrences.setdefault(signature, set()).add(page_number)
    margins = {signature for signature, pages in occurrences.items() if len(pages) >= max(2, ceil(len(pdf.pages) * .5))}
    return PdfTextLayout(body, headings, bookmarks, margins=margins, page_sizes=page_sizes, incomplete_pages=incomplete)


def margin_signature(line: PdfLine, height: float) -> tuple[str, str, int] | None:
    """仅在顶部一成和底部百分之七区域统计重复行，数字归一化以识别动态页码。"""
    zone = 'header' if line.bbox[3] <= height * .10 else 'footer' if line.bbox[1] >= height * .93 else None
    if zone is None:
        return None
    text = re.sub(r'\d+', '#', normalized_title(line.text))
    return zone, text, round(line.bbox[1] / height * 50)


def margin_line(line: PdfLine, height: float, page: int, layout: PdfTextLayout) -> bool:
    """剔除多页确认的页边重复行及独立页码，正文区域的数字和标题保持原样。"""
    signature = margin_signature(line, height)
    if (signature is None or line.size > layout.body_size * 1.07
            or (page, normalized_title(line.text)) in layout.bookmarks):
        return False
    return signature in layout.margins or re.fullmatch(
        r'(?:第\s*\d+\s*页(?:\s*[/／]\s*共?\s*\d+\s*页?)?|(?:page\s*)?\d+(?:\s*[/／]\s*\d+)?|[-—]\s*\d+\s*[-—])',
        line.text, re.I) is not None


def text_line(value: dict[str, Any]) -> PdfLine:
    """将带字符的版面行压缩为轻量记录，正文文字使用版面读取顺序。"""
    chars = [char for char in value.get('chars', []) if char.get('text', '').strip()]
    size = median(float(char['size']) for char in chars) if chars else float(value['bottom'] - value['top'])
    bold = bool(chars) and sum('bold' in str(char.get('fontname', '')).lower() for char in chars) > len(chars) / 2
    bbox = (round(value['x0'], 3), round(value['top'], 3), round(value['x1'], 3), round(value['bottom'], 3))
    return PdfLine(value['text'].strip(), bbox, size, bold)


def list_line(text: str) -> bool:
    """识别常见项目符号与中英文序号，以保留列表项及其缩进续行。"""
    return re.match(r'^(?:[•●▪]|[（(][一二三四五六七八九十\d]+[）)]|\d+[.)、]\s|[一二三四五六七八九十]+、)', text) is not None


def caption_line(text: str) -> bool:
    """复用中英文图注识别并识别表题，使说明行两侧均保持独立边界。"""
    return figure_caption(text) is not None or re.match(r'^(?:表|table)\s*\d', text, re.I) is not None


def merge_page_paragraphs(units: list[DocumentUnit], layout: PdfTextLayout) -> list[DocumentUnit]:
    """保守连接同章节的页尾与下一页顶部正文，句末、列表、表格和标题均阻断衔接。"""
    result: list[DocumentUnit] = []
    for unit in units:
        previous = result[-1] if result else None
        if (previous is not None and previous.kind == unit.kind == 'paragraph'
                and previous.regions and unit.regions and previous.section == unit.section
                and previous.regions[-1].page + 1 == unit.regions[0].page
                and not list_line(previous.content) and not list_line(unit.content)
                and not caption_line(previous.content.strip()) and not caption_line(unit.content.strip())
                and re.search(r'[。！？!?；;：:.][”’"\')）\]]*$', previous.content.strip()) is None):
            last, first = previous.regions[-1], unit.regions[0]
            previous_width, previous_height = layout.page_sizes[last.page]
            next_width, next_height = layout.page_sizes[first.page]
            if (last.bbox[3] >= previous_height * .85 and last.bbox[2] >= previous_width * .8
                    and first.bbox[1] <= next_height * .15 and first.bbox[0] <= next_width * .2):
                previous.content = join_lines(previous.content.rstrip(), unit.content.lstrip())
                previous.end_page = unit.end_page or unit.page
                previous.regions.extend(unit.regions)
                previous.location = f'第 {previous.page}–{previous.end_page} 页正文'
                continue
        result.append(unit)
    return result


def heading_level(line: PdfLine, page: int, layout: PdfTextLayout, isolated: bool) -> int | None:
    """书签优先；短标题组合字号、加粗、间距及编号判级，图注和列表不提升为章节。"""
    bookmark = layout.bookmarks.get((page, normalized_title(line.text)))
    if bookmark is not None:
        return bookmark
    if len(line.text) > 100 or caption_line(line.text):
        return None
    if list_line(line.text):
        return None
    large = line.size > layout.body_size * 1.07
    if large or (line.bold and isolated and line.size >= layout.body_size * .98):
        numbered = re.match(r'^(\d+(?:\.\d+)+)\s+\S', line.text)
        if numbered:
            return min(numbered[1].count('.') + 1, 6)
        for index, size in enumerate(layout.heading_sizes, 1):
            if line.size >= size - layout.body_size * .08:
                return min(index, 6)
        return min(len(layout.heading_sizes) + 1, 6)
    return None


def join_lines(left: str, right: str) -> str:
    """合并段内排版换行；中文及被截断的标识符直接衔接，完整英文词保留空格。"""
    if not left:
        return right
    if not right:
        return left
    if re.search(r'[\u3400-\u9fff，。；：！？、）】]$', left) or re.match(r'^[\u3400-\u9fff，。；：！？、（【]', right):
        return left + right
    if re.search(r'[\u3400-\u9fff][A-Za-z_]+$', left) and re.match(r'^[A-Za-z_]+[\u3400-\u9fff]', right):
        return left + right
    return left + ' ' + right


def paragraph_units(lines: list[PdfLine], page: int, layout: PdfTextLayout) -> list[DocumentUnit]:
    """按标题、行间距与首行缩进恢复自然段，并保留每段的页面矩形位置。"""
    if not lines:
        return []
    units: list[DocumentUnit] = []
    pending: list[PdfLine] = []
    gaps = [after.bbox[1] - before.bbox[3] for before, after in zip(lines, lines[1:])
            if 0 <= after.bbox[1] - before.bbox[3] <= layout.body_size]
    line_gap = median(gaps) if gaps else layout.body_size * .5
    left_margin = min(line.bbox[0] for line in lines)
    right_margin = max(line.bbox[2] for line in lines)

    def flush() -> None:
        """发布一个自然段，记录其章节路径和所有行覆盖的原文区域。"""
        if not pending:
            return
        text = ''
        for line in pending:
            text = join_lines(text, line.text)
        bbox = (min(line.bbox[0] for line in pending), pending[0].bbox[1],
                max(line.bbox[2] for line in pending), pending[-1].bbox[3])
        units.append(DocumentUnit(page=page, end_page=page, paragraph=0, content=text + '\n\n',
                                  section=[title for _, title in layout.section], location=f'第 {page} 页正文',
                                  regions=[DocumentRegion(page=page, bbox=bbox)]))
        pending.clear()

    for index, line in enumerate(lines):
        before = line.bbox[1] - lines[index - 1].bbox[3] if index else float('inf')
        after = lines[index + 1].bbox[1] - line.bbox[3] if index + 1 < len(lines) else float('inf')
        level = heading_level(line, page, layout, before > line_gap * 1.5 and after > line_gap * 1.2)
        if level is not None:
            flush()
            layout.section = [(depth, title) for depth, title in layout.section if depth < level]
            layout.section.append((level, line.text))
            units.append(DocumentUnit(page=page, end_page=page, paragraph=0, content=line.text + '\n\n',
                                      kind='heading', heading_level=level,
                                      section=[title for _, title in layout.section], location=f'第 {page} 页标题',
                                      regions=[DocumentRegion(page=page, bbox=line.bbox)]))
            continue
        if pending:
            previous = pending[-1]
            gap = line.bbox[1] - previous.bbox[3]
            indented = line.bbox[0] > left_margin + layout.body_size * .9 and previous.bbox[0] < line.bbox[0] - layout.body_size * .6
            list_continuation = list_line(pending[0].text) and line.bbox[0] > pending[0].bbox[0] + layout.body_size * .5
            separate_short_line = (abs(line.bbox[0] - previous.bbox[0]) < layout.body_size * .4
                                   and previous.bbox[2] < right_margin - layout.body_size * 3
                                   and gap >= line_gap * .9)
            special = list_line(line.text) or caption_line(line.text) or caption_line(previous.text)
            if (gap > max(line_gap * 1.65, line_gap + 3) or special
                    or ((indented or separate_short_line) and not list_continuation)):
                flush()
        pending.append(line)
    flush()
    return units
