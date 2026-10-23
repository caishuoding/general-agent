"""保存会话文档共用切片及位置元数据，不另存解析正文，也不接触长期记忆。"""

import os
import re
from pathlib import Path
from math import log
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from psycopg import sql

from general_agent.infrastructure.tokens import TokenCounter
from general_agent.infrastructure.settings import SettingsStore

MAX_DOCUMENT_UNITS = 20_000


class DocumentRegion(BaseModel):
    """记录物理页中以左上角为原点、单位为 point 的原文矩形区域。"""

    page: int
    bbox: tuple[float, float, float, float]


class TableCell(BaseModel):
    """保存零起始行列的真实单元格和合并跨度，文字始终作为普通文本处理。"""

    row: int = Field(ge=0, le=20000)
    column: int = Field(ge=0, le=255)
    text: str
    rowspan: int = Field(default=1, ge=1, le=20000)
    colspan: int = Field(default=1, ge=1, le=256)
    header: bool = False


class TableFragment(BaseModel):
    """保存实际切片包含的表格行与重复表头，供引用展示而不扩展为整张未召回表格。"""

    table_id: int
    title: str | None = None
    columns: int = Field(ge=1, le=256)
    rows: int = Field(ge=1, le=20000)
    header_rows: int = Field(default=0, ge=0, le=20000)
    cells: list[TableCell] = Field(default_factory=list, max_length=20000)
    source_rows: list[int] = Field(default_factory=list)
    continued: bool = False
    render_as_image: bool = False
    image_id: UUID | None = None


class DocumentUnit(BaseModel):
    """保存标题、段落、表格行及章节；PDF 额外保留各物理页的原文区域。"""

    page: int | None = None
    paragraph: int
    content: str
    kind: Literal['paragraph', 'heading', 'table_row', 'image'] = 'paragraph'
    heading_level: int | None = None
    table: int | None = None
    row: int | None = None
    section: list[str] = Field(default_factory=list)
    location: str | None = None
    end_page: int | None = None
    regions: list[DocumentRegion] = Field(default_factory=list)
    table_cells: list[TableCell] = Field(default_factory=list)
    table_header: bool = False
    table_columns: int | None = None
    table_title: str | None = None
    table_continued: bool = False
    table_image_id: UUID | None = None


def default_chunk_counter() -> TokenCounter:
    """为未绑定应用配置的调用提供显式标记的保守 Token 计数，不猜用其他模型分词器。"""
    return TokenCounter('document-default')


@dataclass(frozen=True)
class ChunkPolicy:
    """管理 Token 软上限、完整块容差和正文重叠，不再使用字符数作为切片预算。"""

    tokens: int = 800
    results: int = 5
    overlap: int = 160
    overflow: int = 200
    counter: TokenCounter = field(default_factory=default_chunk_counter, compare=False, repr=False)

    def __post_init__(self) -> None:
        """校验预算，并将正文重叠限制在目标 Token 数的百分之二十以内。"""
        if not 100 <= self.tokens <= 10000 or not 1 <= self.results <= 50:
            raise ValueError('切片目标需为 100–10000 Token，返回片数需为 1–50。')
        if not 0 <= self.overlap <= 10000 or not 0 <= self.overflow <= 200:
            raise ValueError('重叠需为 0–10000 Token，完整块容差需为 0–200 Token。')
        object.__setattr__(self, 'overlap', min(self.overlap, self.tokens // 5))

    @property
    def max_tokens(self) -> int:
        """返回包含重叠和表头在内的绝对 Token 上限。"""
        return self.tokens + self.overflow

    @property
    def signature(self) -> str:
        """标识实际切片规则与计数快照，供结果追溯，不要求随 LLM 切换重切旧文档。"""
        return f'{self.layout_signature}:{self.counter.fingerprint}'

    @property
    def layout_signature(self) -> str:
        """仅标识切片结构和预算规则，使 LLM 切换不会单独触发旧文档重切。"""
        return f'markdown-token-v1:{self.tokens}:{self.overflow}:{self.overlap}'

    def end(self, text: str, start: int, budget: int, stop: int) -> int:
        """返回预算内的字符结束位置，逐步扩展窗口，避免反复分词整个长文档。"""
        if budget <= 0:
            return start
        limit = min(stop, start + max(1, budget))
        while limit < stop and self.counter.count(text[start:limit]) <= budget:
            limit = min(stop, start + (limit - start) * 2)
        return start + len(self.counter.prefix(text[start:limit], budget))


def configured_chunk_policy(directory: Path) -> ChunkPolicy:
    """按当前回答 LLM 创建独立计数快照，附件与知识库共用该选择规则。"""
    settings = SettingsStore(directory).read()
    override = os.environ.get('DOCUMENT_CHUNK_TOKENIZER_MODEL')
    if settings is not None and override:
        settings = settings.model_copy(update={'model': override})
    counter = TokenCounter.for_settings(settings, directory)
    return ChunkPolicy(int(os.environ.get('DOCUMENT_CHUNK_TOKENS', '800')),
                       int(os.environ.get('DOCUMENT_SEARCH_RESULTS', '5')),
                       int(os.environ.get('DOCUMENT_CHUNK_OVERLAP_TOKENS', '160')),
                       int(os.environ.get('DOCUMENT_CHUNK_OVERFLOW_TOKENS', '200')), counter)


def text_units(text: str, page: int | None = None) -> list[DocumentUnit]:
    """按空行识别自然段并保留全部字符；单换行留在段内，预先限制行数。"""
    breaks = sum(text.count(separator) for separator in ('\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')) - text.count('\r\n')
    if breaks > MAX_DOCUMENT_UNITS:
        raise ValueError('文档段落数量超过 20,000 个资源限制，请拆分后上传。')
    ends = [match.end() for match in re.finditer(r'(?:\r\n|(?<!\r)\n|\r(?!\n))[ \t]*(?:(?:\r\n|(?<!\r)\n|\r(?!\n))[ \t]*)+', text)]
    boundaries = [0, *ends, len(text)]
    parts = [text[start:end] for start, end in zip(boundaries, boundaries[1:]) if end > start] or ['']
    if len(parts) > MAX_DOCUMENT_UNITS:
        raise ValueError('文档段落数量超过 20,000 个资源限制，请拆分后上传。')
    return [DocumentUnit(page=page, paragraph=index, content=part)
            for index, part in enumerate(parts, 1)]


def sentence_ends(text: str) -> list[int]:
    """识别中英文句末并带上闭引号及空白，避开常见英文缩写与数字小数点。"""
    ends: list[int] = []
    for match in re.finditer(r'''[。！？!?]+[”’"'）)\]]*\s*|\.(?=[\s"'”’]|$)[”’"')\]]*\s*''', text):
        if text[match.start()] == '.':
            word = re.search(r'[A-Za-z.]+$', text[max(0, match.start() - 32):match.start()])
            if word and (word[0].lower() in {'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'e.g', 'i.e'}
                         or len(word[0]) == 1):
                continue
        ends.append(match.end())
    return sorted(set([*ends, len(text)]))


def chunk_ranges(text: str, policy: ChunkPolicy, paragraph_ends: list[int] | None = None) -> list[tuple[int, int]]:
    """按 Token 软上限递归选择自然边界，最多使用两百容差保留完整块，重叠不超过两成。"""
    paragraphs = sorted(set(paragraph_ends if paragraph_ends is not None else
                            [match.end() for match in re.finditer(r'\n[ \t]*\n\s*', text)]))
    sentences = sentence_ends(text)
    sentence_boundaries = set(sentences)
    clauses = [match.end() for match in re.finditer(r'[；;，,、：:]+[ \t]*', text)]
    spaces = [match.end() for match in re.finditer(r'\s+', text)]
    sections = sorted({0, len(text), *[match.start() for match in re.finditer(
        r'(?m)^[ \t]{0,3}(?:#{1,6}[ \t]+[^\n]+|第[一二三四五六七八九十百千零〇\d]+[章节篇部][^\n]*)', text)]})
    ranges: list[tuple[int, int]] = []
    for section_start, section_end in zip(sections, sections[1:]):
        start = previous_end = section_start
        while previous_end < section_end:
            soft = policy.end(text, start, policy.tokens, section_end)
            hard = policy.end(text, start, policy.max_tokens, section_end)
            next_sentence = sentences[bisect_right(sentences, previous_end)]
            if hard < next_sentence <= policy.end(text, previous_end, policy.max_tokens, section_end):
                start = previous_end
                soft = policy.end(text, start, policy.tokens, section_end)
                hard = policy.end(text, start, policy.max_tokens, section_end)
            if soft <= previous_end:
                start = previous_end
                soft = policy.end(text, start, policy.tokens, section_end)
                hard = policy.end(text, start, policy.max_tokens, section_end)
            if hard == section_end:
                end = section_end
            else:
                end = soft
                for boundaries, extend in ((paragraphs, True), (sentences, True), (clauses, False), (spaces, False)):
                    following = bisect_left(boundaries, soft)
                    if extend and following < len(boundaries) and previous_end < boundaries[following] <= hard:
                        end = boundaries[following]
                        break
                    preceding = bisect_right(boundaries, soft) - 1
                    if preceding >= 0 and boundaries[preceding] > previous_end and text[previous_end:boundaries[preceding]].strip():
                        end = boundaries[preceding]
                        break
            if end <= previous_end:
                raise ValueError('Token 预算不足以容纳一个原文字符。')
            ranges.append((start, end))
            previous_end = end
            if not policy.overlap:
                start = end
                continue
            candidates = [start, *sentences[bisect_right(sentences, start):bisect_left(sentences, end)]]
            fitting = [boundary for boundary in candidates if policy.counter.count(text[boundary:end]) <= policy.overlap]
            if fitting:
                start = fitting[0]
            elif len(candidates) > 1 or end in sentence_boundaries:
                start = end
            else:
                # 超长无句末片段退到预算内尾部，再优先对齐分句或空白。
                low, high = start, end
                while low < high:
                    middle = (low + high) // 2
                    if policy.counter.count(text[middle:end]) <= policy.overlap:
                        high = middle
                    else:
                        low = middle + 1
                start = low
                for boundaries in (clauses, spaces):
                    index = bisect_left(boundaries, start)
                    if index < len(boundaries) and boundaries[index] < end:
                        start = boundaries[index]
                        break
    return ranges


def search_terms(text: str) -> list[str]:
    """提取英文词、中文单字及相邻双字词，构建无外部依赖的中英文倒排索引。"""
    terms: list[str] = re.findall(r'[a-z0-9_]+', text.casefold())
    for sequence in re.findall(r'[\u3400-\u9fff]+', text):
        terms.extend(sequence)
        terms.extend(sequence[index:index + 2] for index in range(max(1, len(sequence) - 1)))
    return terms


@dataclass(frozen=True)
class ChunkSpec:
    """以原文范围和结构性包装描述切片；重复表头不冒充段内原文偏移。"""

    start: int
    end: int
    prefix: str = ''
    suffix: str = ''



def markdown_cell_end(text: str, start: int, end: int, stop: int) -> int:
    """将续片边界退到 Markdown 转义、HTML 实体和换行标记之外，避免拆坏单元格语法。"""
    for match in re.compile(r'\\.|&(?:amp|lt|gt);|<br>').finditer(text, start, min(stop, end + 6)):
        left, right = match.start(), match.end()
        if left < end < right:
            return left
        if left >= end:
            break
    return end


def table_specs(group: list[DocumentUnit], offset: int, policy: ChunkPolicy) -> list[ChunkSpec]:
    """按完整 Markdown 行分片且重复表头；超长单行按单元格续片，包装均计入预算。"""
    text = ''.join(unit.content for unit in group)
    header = group[0].content
    if policy.counter.count(text) <= policy.max_tokens:
        return [ChunkSpec(offset, offset + len(text))]
    if policy.counter.count(header) >= policy.max_tokens:
        raise ValueError('表头已达到切片 Token 上限，请简化过宽的表格。')
    specs: list[ChunkSpec] = []
    cursor, index = len(header), 1
    while index < len(group):
        row = group[index].content
        if policy.counter.count(header + row) > policy.max_tokens:
            # 单行超限时保留列位置，每个片段只续写一列，其余列留空，不重复数据行。
            pipes = [match.start() for match in re.finditer(r'(?<!\\)\|', row)]
            columns = len(pipes) - 1
            for column, (left, right) in enumerate(zip(pipes, pipes[1:])):
                start, stop = left + 1, right
                prefix = header + '| ' + ' | ' * column
                suffix = ' | ' * (columns - column - 1) + ' |\n'
                overhead = policy.counter.count(prefix + suffix)
                if overhead >= policy.max_tokens:
                    raise ValueError('表头和列结构已达到切片 Token 上限，请简化表格。')
                while start < stop:
                    end = policy.end(row, start, policy.tokens - overhead, stop)
                    if end == start:
                        end = policy.end(row, start, policy.max_tokens - overhead, stop)
                    while end > start and policy.counter.count(prefix + row[start:end] + suffix) > policy.max_tokens:
                        end -= 1
                    end = markdown_cell_end(row, start, end, stop)
                    if end <= start:
                        raise ValueError('表格包装后没有足够 Token 空间容纳单元格。')
                    specs.append(ChunkSpec(offset + cursor + start, offset + cursor + end, prefix, suffix))
                    start = end
            cursor += len(row)
            index += 1
            continue
        end, body = cursor, ''
        while index < len(group):
            row = group[index].content
            if policy.counter.count(header + body + row) > policy.max_tokens:
                break
            body += row
            end += len(row)
            index += 1
            if policy.counter.count(header + body) >= policy.tokens:
                break
        specs.append(ChunkSpec(offset + cursor, offset + end, header))
        cursor = end
    return specs


def chunk_specs(units: list[DocumentUnit], policy: ChunkPolicy) -> list[ChunkSpec]:
    """按章节、表格和媒体位置断开正文，再沿自然切点分片并单独保存结构包装。"""
    specs: list[ChunkSpec] = []
    offset, index = 0, 0
    native = any(unit.location is not None for unit in units)
    while index < len(units):
        first = units[index]
        group: list[DocumentUnit] = []
        while index < len(units):
            unit = units[index]
            if native and group and (unit.kind in ('heading', 'image') or first.kind == 'image'
                                     or unit.section != first.section or unit.table != first.table):
                break
            group.append(unit)
            index += 1
        text = ''.join(unit.content for unit in group)
        if native and first.table is not None:
            specs.extend(table_specs(group, offset, policy))
        else:
            ends: list[int] = []
            for unit in group:
                ends.append((ends[-1] if ends else 0) + len(unit.content))
            specs.extend(ChunkSpec(offset + start, offset + end) for start, end in chunk_ranges(text, policy, ends))
        offset += len(text)
    return specs


def unit_position(unit: DocumentUnit) -> tuple[int, float, float]:
    """有坐标时按物理位置排序，否则沿用 Word 的页码和段落位置。"""
    if unit.regions:
        return unit.regions[0].page, unit.regions[0].bbox[1], unit.regions[0].bbox[0]
    return unit.page or 1, float(unit.paragraph), 0


def media_boundaries(units: list[DocumentUnit], images: list[dict[str, Any]]) -> list[DocumentUnit]:
    """在图片所在位置插入不持久化正文的边界，防止一个正文片跨过图片。"""
    ordered = list(units)
    for image in images:
        if image.get('media_type') == 'table_preview':
            continue
        page = image.get('page') or 1
        regions = [DocumentRegion(page=page, bbox=image['bbox'])] if image.get('bbox') else []
        ordered.append(DocumentUnit(page=page, end_page=page, paragraph=image['paragraph'], content='', kind='image',
                                    section=image.get('section', []), location=image['location'], regions=regions))
    return sorted(ordered, key=unit_position)


def table_chunk_fragment(all_units: list[DocumentUnit], selected: list[DocumentUnit], table_image_id: UUID | None) -> dict[str, Any] | None:
    """构造仅含表头与选中行的片段，并以片段内相对行号避免跨页覆盖。"""
    if not selected or selected[0].table is None:
        return None
    table_id = selected[0].table
    group = [unit for unit in all_units if unit.table == table_id]
    first_page = group[0].page
    headers = [unit for unit in group if unit.table_header and unit.page == first_page]
    # 跨页重复表头在正文中为空；即使解析器漏标 header，也不能把它当成数据行。
    body = [unit for unit in selected if not unit.table_header and unit.content.strip()]
    included = headers + body
    remapped: list[TableCell] = []
    row_offset = 0
    # 同一页内保留解析器的行跨度关系，跨页则连续重排为片段内唯一的行坐标。
    for unit in included:
        unit_rows = sorted({cell.row for cell in unit.table_cells})
        row_map = {source_row: row_offset + index for index, source_row in enumerate(unit_rows)}
        remapped.extend(cell.model_copy(update={'row': row_map[cell.row]}) for cell in unit.table_cells)
        row_offset += len(unit_rows)
    if not remapped:
        return None
    header_cells = [cell for cell in remapped if cell.header]
    fragment = TableFragment(
        table_id=table_id,
        title=next((unit.table_title for unit in group if unit.table_title), None),
        columns=int(selected[0].table_columns or max(cell.column + cell.colspan for cell in remapped)),
        rows=row_offset,
        header_rows=len({cell.row for cell in header_cells}),
        cells=remapped,
        source_rows=[unit.row or index for index, unit in enumerate(body, 1)],
        continued=any(unit.table_continued for unit in group),
        render_as_image=bool(table_image_id),
        image_id=table_image_id,
    )
    return fragment.model_dump(mode='json', exclude_defaults=True)


def store_chunks(connection: Any, identifier: UUID, units: list[DocumentUnit], policy: ChunkPolicy,
                 *, knowledge: bool = False, images: list[dict[str, Any]] | None = None) -> None:
    """保存正文与原文区域，媒体存在时先按位置分组；表名仅由内部白名单选择。"""
    if images:
        units = media_boundaries(units, images)
    table, parent = ('knowledge_chunks', 'document_id') if knowledge else ('document_chunks', 'attachment_id')
    connection.execute(sql.SQL('DELETE FROM {} WHERE {}=%s').format(sql.Identifier(table), sql.Identifier(parent)), (identifier,))
    chunks: list[tuple[Any, ...]] = []
    ends: list[int] = []
    total = 0
    for unit in units:
        total += len(unit.content)
        ends.append(total)
    text = ''.join(unit.content for unit in units)
    for spec in chunk_specs(units, policy):
        first, last = bisect_right(ends, spec.start), bisect_left(ends, spec.end)
        content = spec.prefix + text[spec.start:spec.end] + spec.suffix
        if not content.strip():
            continue
        if policy.counter.count(content) > policy.max_tokens:
            raise ValueError('切片正文和结构包装超过 Token 上限。')
        start_unit, end_unit = units[first], units[last]
        structure = start_unit.model_dump(exclude={'page', 'paragraph', 'content', 'table_image_id'}, exclude_none=True, exclude_defaults=True)
        regions = list(dict.fromkeys((region.page, region.bbox) for unit in units[first:last + 1]
                                     if unit.content.strip() for region in unit.regions))
        if regions:
            structure['regions'] = [DocumentRegion(page=page, bbox=bbox).model_dump() for page, bbox in regions]
        if start_unit.table is not None:
            fragment = table_chunk_fragment(units, units[first:last + 1], start_unit.table_image_id)
            if fragment is not None:
                structure['table'] = fragment
        chunks.append((identifier, len(chunks) + 1, start_unit.page, end_unit.end_page or end_unit.page,
                       start_unit.paragraph, end_unit.paragraph,
                       Jsonb(structure),
                       Jsonb(end_unit.model_dump(exclude={'page', 'paragraph', 'content', 'table_image_id'}, exclude_none=True, exclude_defaults=True)),
                       content, sorted(set(search_terms(content))), spec.prefix, spec.suffix,
                       policy.counter.count(content), policy.counter.method))
    if chunks:
        with connection.cursor() as cursor:
            cursor.executemany(
                sql.SQL('INSERT INTO {}({},ordinal,page,end_page,paragraph,end_paragraph,structure,end_structure,'
                'content,terms,context_prefix,context_suffix,token_count,token_count_method) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)').format(sql.Identifier(table), sql.Identifier(parent)), chunks,
            )


def store_document_images(
    connection: Any,
    identifier: UUID,
    images: list[dict[str, Any]],
) -> None:
    """重建图片元数据并仅保存纯文字描述，旧结构化字段沿用数据库空默认值。"""
    connection.execute('DELETE FROM document_images WHERE attachment_id=%s', (identifier,))
    if not images:
        return
    rows = [
        (
            item['id'], identifier, item['ordinal'], item.get('page'), item.get('end_page'),
            item['paragraph'], Jsonb(item.get('section', [])), item['location'],
            item['storage_name'], item['sha256'], item['mime'], item['width'], item['height'],
            item.get('caption') or '', item.get('description', ''),
            Jsonb(item['provider_file']) if item.get('provider_file') else None,
            item['vision_status'], item.get('vision_model'), item.get('vision_revision'), item.get('vision_error'),
        )
        for item in images
    ]
    with connection.cursor() as cursor:
        cursor.executemany(
            'INSERT INTO document_images('
            'id,attachment_id,ordinal,page,end_page,paragraph,section,location,storage_name,sha256,mime,'
            'width,height,caption,description,provider_file,vision_status,'
            'vision_model,vision_revision,vision_error'
            ') VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            rows,
        )


def store_image_chunks(connection: Any, identifier: UUID, images: list[dict[str, Any]], policy: ChunkPolicy,
                       *, knowledge: bool = False) -> None:
    """保存绑定原图的描述切片，再把所有类型切片统一恢复为原文位置顺序。"""
    table, parent = ('knowledge_chunks', 'document_id') if knowledge else ('document_chunks', 'attachment_id')
    next_ordinal = (connection.execute(
        sql.SQL('SELECT COALESCE(MAX(ordinal),0)+1 AS ordinal FROM {} WHERE {}=%s').format(sql.Identifier(table), sql.Identifier(parent)),
        (identifier,),
    ).fetchone() or {'ordinal': 1})['ordinal']
    rows: list[tuple[Any, ...]] = []
    image_policy = replace(policy, overlap=0)
    for image in images:
        if image.get('vision_status') != 'complete' or image.get('media_type') == 'table_preview':
            continue
        content = image['description']
        metadata = {'kind': 'image', 'ordinal': image['ordinal'], 'caption': image.get('caption'),
                    'section': image.get('section', []), 'location': image['location']}
        if image.get('bbox'):
            metadata['regions'] = [{'page': image.get('page') or 1, 'bbox': image['bbox']}]
        structure = Jsonb(metadata)
        for start, end in chunk_ranges(content, image_policy):
            chunk = content[start:end]
            if not chunk.strip():
                continue
            rows.append((
                identifier, next_ordinal, image.get('page') or 1, image.get('end_page') or image.get('page') or 1,
                image['paragraph'], image['paragraph'], structure, structure, chunk,
                sorted(set(search_terms(chunk))), '', '', image['id'], image_policy.counter.count(chunk),
                image_policy.counter.method,
            ))
            next_ordinal += 1
    if rows:
        with connection.cursor() as cursor:
            cursor.executemany(
                sql.SQL('INSERT INTO {}('
                '{},ordinal,page,end_page,paragraph,end_paragraph,structure,end_structure,'
                'content,terms,context_prefix,context_suffix,image_id,token_count,token_count_method'
                ') VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)').format(sql.Identifier(table), sql.Identifier(parent)),
                rows,
            )
        reorder_chunks(connection, identifier, knowledge=knowledge)


def chunk_position(row: dict[str, Any]) -> tuple[int, float, float, int]:
    """按保存的区域排序切片，同一图片的描述续片用旧序号保持相邻顺序。"""
    regions = row['structure'].get('regions', [])
    if regions:
        return regions[0]['page'], regions[0]['bbox'][1], regions[0]['bbox'][0], row['ordinal']
    return row['page'] or 1, float(row['paragraph']), 0, row['ordinal']


def reorder_chunks(connection: Any, identifier: UUID, *, knowledge: bool = False) -> None:
    """在同一入库事务内借助临时正序号避开唯一键冲突，统一正文、表格和图片顺序。"""
    table, parent = ('knowledge_chunks', 'document_id') if knowledge else ('document_chunks', 'attachment_id')
    rows = connection.execute(sql.SQL('SELECT ordinal,page,paragraph,structure FROM {} WHERE {}=%s ORDER BY ordinal')
                              .format(sql.Identifier(table), sql.Identifier(parent)), (identifier,)).fetchall()
    if not rows:
        return
    offset = max(row['ordinal'] for row in rows) + len(rows) + 1
    connection.execute(sql.SQL('UPDATE {} SET ordinal=ordinal+%s WHERE {}=%s')
                       .format(sql.Identifier(table), sql.Identifier(parent)), (offset, identifier))
    with connection.cursor() as cursor:
        cursor.executemany(sql.SQL('UPDATE {} SET ordinal=%s WHERE {}=%s AND ordinal=%s')
                           .format(sql.Identifier(table), sql.Identifier(parent)),
                           [(index, identifier, row['ordinal'] + offset) for index, row in enumerate(sorted(rows, key=chunk_position), 1)])


def rank_chunks(rows: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    """以 BM25（k1=1.5、b=0.75）排序授权范围内的切片，只返回正分命中。"""
    terms = set(search_terms(query))
    if not rows or not terms:
        return []
    frequencies = [Counter(search_terms(row['content'])) for row in rows]
    average = sum(sum(counts.values()) for counts in frequencies) / len(rows)
    if not average:
        return []
    document_counts = Counter(term for counts in frequencies for term in terms if term in counts)
    scored: list[tuple[float, int]] = []
    for index, counts in enumerate(frequencies):
        length = sum(counts.values())
        score = 0.0
        for term in terms:
            frequency = counts[term]
            if frequency:
                inverse = log(1 + (len(rows) - document_counts[term] + 0.5) / (document_counts[term] + 0.5))
                score += inverse * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / average))
        if score > 0:
            scored.append((score, index))
    scored.sort(key=bm25_sort_key)
    return [rows[index] for _, index in scored[:limit]]


def bm25_sort_key(item: tuple[float, int]) -> tuple[float, int]:
    """按分数降序排序，同分时保持数据库中的切片次序。"""
    return -item[0], item[1]
