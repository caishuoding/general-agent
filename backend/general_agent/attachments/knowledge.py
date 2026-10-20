"""保存会话文档的结构原文和独立切片索引，不接触长期记忆。"""

import re
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
    """将切片字符数和检索返回片数集中管理，不添加检索 Token 预算。"""

    characters: int = 1500
    results: int = 5

    def __post_init__(self) -> None:
        """拒绝会导致无进展分片或无界返回的策略参数。"""
        if not 100 <= self.characters <= 10000 or not 1 <= self.results <= 50:
            raise ValueError('切片字符数需为 100–10000，返回片数需为 1–50。')


def text_units(text: str, page: int | None = None) -> list[DocumentUnit]:
    """按换行保留全文字符及空行，先限段落数，避免短行文件耗尽服务进程内存。"""
    breaks = sum(text.count(separator) for separator in ('\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029')) - text.count('\r\n')
    if breaks > MAX_DOCUMENT_UNITS:
        raise ValueError('文档段落数量超过 20,000 个资源限制，请拆分后上传。')
    parts = text.splitlines(keepends=True) or ['']
    if len(parts) > MAX_DOCUMENT_UNITS:
        raise ValueError('文档段落数量超过 20,000 个资源限制，请拆分后上传。')
    return [DocumentUnit(page=page, paragraph=index, content=part)
            for index, part in enumerate(parts, 1)]


def search_terms(text: str) -> list[str]:
    """提取英文词、中文单字及相邻双字词，构建无外部依赖的中英文倒排索引。"""
    terms: list[str] = re.findall(r'[a-z0-9_]+', text.casefold())
    for sequence in re.findall(r'[\u3400-\u9fff]+', text):
        terms.extend(sequence)
        terms.extend(sequence[index:index + 2] for index in range(max(1, len(sequence) - 1)))
    return terms


def store_units(connection: Any, identifier: UUID, units: list[DocumentUnit], policy: ChunkPolicy) -> None:
    """在调用方事务内原子替换原文和切片，保存切片在原段落内的准确范围。"""
    connection.execute('DELETE FROM document_units WHERE attachment_id=%s', (identifier,))
    chunks: list[tuple[Any, ...]] = []
    for ordinal, unit in enumerate(units, 1):
        connection.execute(
            'INSERT INTO document_units(attachment_id,ordinal,page,paragraph,content) VALUES(%s,%s,%s,%s,%s)',
            (identifier, ordinal, unit.page, unit.paragraph, unit.content),
        )
        for offset in range(0, len(unit.content), policy.characters):
            content = unit.content[offset:offset + policy.characters]
            if content.strip():
                chunks.append((identifier, len(chunks) + 1, ordinal, offset, offset + len(content), content,
                               sorted(set(search_terms(content)))))
    if chunks:
        with connection.cursor() as cursor:
            cursor.executemany(
                'INSERT INTO document_chunks(attachment_id,ordinal,unit_ordinal,start_offset,end_offset,content,terms) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s)', chunks,
            )


def rank_chunks(rows: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    """按查询词覆盖和词频排序候选切片，稳定返回指定片数的完整正文。"""
    terms = set(search_terms(query))

    def score(row: dict[str, Any]) -> float:
        """提高查询覆盖与原始短语匹配权重，避免长切片仅凭词数占优。"""
        frequencies = Counter(search_terms(row['content']))
        coverage = len(terms.intersection(frequencies)) / max(1, len(terms))
        frequency = sum(min(frequencies[term], 3) for term in terms) / max(1, len(frequencies))
        return coverage + frequency + (2 if query.casefold() in row['content'].casefold() else 0)

    return sorted(rows, key=score, reverse=True)[:limit]
