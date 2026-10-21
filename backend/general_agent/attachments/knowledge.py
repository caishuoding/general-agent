"""保存会话文档的结构原文和独立切片索引，不接触长期记忆。"""

import re
from math import log
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel

MAX_DOCUMENT_UNITS = 20_000


class DocumentUnit(BaseModel):
    """表示一个页内段落；非分页文字使用全局段落编号，空白页仍保留位置。"""

    page: int | None = None
    paragraph: int
    content: str


@dataclass(frozen=True)
class ChunkPolicy:
    """管理包含重叠的切片上限、重叠目标及检索片数，不添加 Token 预算。"""

    characters: int = 1500
    results: int = 5
    overlap: int = 200

    def __post_init__(self) -> None:
        """校验切片、返回数量及重叠目标；实际重叠还受新内容空间限制。"""
        if not 100 <= self.characters <= 10000 or not 1 <= self.results <= 50:
            raise ValueError('切片字符数需为 100–10000，返回片数需为 1–50。')
        if not 0 <= self.overlap <= 10000:
            raise ValueError('切片重叠目标需为 0–10000 字符。')


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
    """递归选择段落、句子、分句、空白边界；重叠整句且每片必须推进原文。"""
    paragraphs = sorted(set(paragraph_ends if paragraph_ends is not None else
                            [match.end() for match in re.finditer(r'\n[ \t]*\n\s*', text)]))
    sentences = sentence_ends(text)
    clauses = [match.end() for match in re.finditer(r'[；;，,、：:]+[ \t]*', text)]
    spaces = [match.end() for match in re.finditer(r'\s+', text)]
    # 章节边界不跨片、不重叠；分页本身不阻断连续正文。
    sections = sorted({0, len(text), *[match.start() for match in re.finditer(
        r'(?m)^[ \t]{0,3}(?:#{1,6}[ \t]+[^\n]+|第[一二三四五六七八九十百千零〇\d]+[章节篇部][^\n]*)', text)]})
    ranges: list[tuple[int, int]] = []
    for section_start, section_end in zip(sections, sections[1:]):
        start = previous_end = section_start
        while previous_end < section_end:
            cap = min(start + policy.characters, section_end)
            next_sentence = sentences[bisect_right(sentences, previous_end)]
            if cap < next_sentence <= min(previous_end + policy.characters, section_end):
                # 重叠导致新句放不下时，先缩减重叠，避免把原本可完整容纳的句子拆碎。
                start = previous_end
                cap = min(start + policy.characters, section_end)
            if cap <= previous_end:
                start = previous_end
                cap = min(start + policy.characters, section_end)
            end = cap
            if cap < section_end:
                for boundaries in (paragraphs, sentences, clauses, spaces):
                    index = bisect_right(boundaries, cap) - 1
                    if index >= 0 and boundaries[index] > previous_end and text[previous_end:boundaries[index]].strip():
                        end = boundaries[index]
                        break
            ranges.append((start, end))
            previous_end = end
            if not policy.overlap:
                start = end
                continue
            # 尾部不足目标时也允许带上一句，但绝不让重叠占满整片。
            lower = max(start, section_start, end - policy.characters + 1)
            candidates = [lower] if lower == start else []
            left = bisect_right(sentences, lower)
            right = bisect_left(sentences, end)
            candidates.extend(sentences[left:right])
            fitting = [boundary for boundary in candidates if end - boundary <= policy.overlap]
            if fitting:
                start = fitting[0]
            elif candidates:
                start = candidates[-1]
            else:
                # 无完整句子可重叠时，使用递归兜底边界或字符片段。
                start = max(lower, end - min(policy.overlap, policy.characters - 1))
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


def store_units(connection: Any, identifier: UUID, units: list[DocumentUnit], policy: ChunkPolicy) -> None:
    """原子替换原文与递归重叠切片，保存跨段、跨页片段两端的准确位置。"""
    connection.execute('DELETE FROM document_units WHERE attachment_id=%s', (identifier,))
    chunks: list[tuple[Any, ...]] = []
    ends: list[int] = []
    total = 0
    for ordinal, unit in enumerate(units, 1):
        connection.execute(
            'INSERT INTO document_units(attachment_id,ordinal,page,paragraph,content) VALUES(%s,%s,%s,%s,%s)',
            (identifier, ordinal, unit.page, unit.paragraph, unit.content),
        )
        total += len(unit.content)
        ends.append(total)
    text = ''.join(unit.content for unit in units)
    for start, end in chunk_ranges(text, policy, ends):
        first, last = bisect_right(ends, start), bisect_left(ends, end)
        start_offset = start - (ends[first - 1] if first else 0)
        end_offset = end - (ends[last - 1] if last else 0)
        content = text[start:end]
        if not content.strip():
            continue
        chunks.append((identifier, len(chunks) + 1, first + 1, last + 1, start_offset, end_offset,
                       content, sorted(set(search_terms(content)))))
    if chunks:
        with connection.cursor() as cursor:
            cursor.executemany(
                'INSERT INTO document_chunks(attachment_id,ordinal,unit_ordinal,end_unit_ordinal,start_offset,end_offset,content,terms) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', chunks,
            )


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
