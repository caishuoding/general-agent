"""提供仅限当前会话的文档检索及有预算的连续原文读取工具。"""

import json
from typing import Any
from uuid import UUID

import httpx

from pydantic import BaseModel, ConfigDict, Field

from general_agent.attachments.knowledge import ChunkPolicy, rank_chunks
from general_agent.attachments.retrieval import DocumentRetrieval, chunk_key, reciprocal_rank_fusion
from general_agent.infrastructure.provider import ModelFailure
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.tokens import TokenCounter


class SearchDocumentsInput(BaseModel):
    """查询可限定当前会话内的文档，返回数量由后台切片策略决定。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=2000)
    document_ids: list[str] | None = Field(default=None, max_length=50)


class ReadCursor(BaseModel):
    """定位原文单元及字符偏移，支持在同一页和段落内继续读取。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    unit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ReadDocumentInput(BaseModel):
    """分页文档按页范围读取，非分页文档按段落范围读取；省略范围读取全文。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    document_id: str
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    start_paragraph: int | None = Field(default=None, ge=1)
    end_paragraph: int | None = Field(default=None, ge=1)
    cursor: ReadCursor | None = None


def document_tools() -> list[dict[str, Any]]:
    """描述两个工具的能力和参数，不规定模型调用时机、任务路由或执行顺序。"""
    definitions: list[tuple[str, str, type[BaseModel]]] = [
        ('search_documents', '检索当前会话文档，返回匹配的完整切片及原文位置；可限定 document_ids。每轮最多五次。', SearchDocumentsInput),
        ('read_document', '按页读取 PDF／Word 的连续原文，TXT／Markdown 按段落读取。省略范围读取全文；超限返回 next_cursor，续读时保持原范围并传入游标。最多 10000 Token 且不超过剩余上下文，每轮最多五次。', ReadDocumentInput),
    ]
    return [{'type': 'function', 'function': {'name': name, 'description': description,
             'parameters': schema.model_json_schema()}} for name, description, schema in definitions]


class DocumentTools:
    """隐藏文档访问权限、切片召回和原文位置，不接受模型指定会话身份。"""

    def __init__(self, database: Database, policy: ChunkPolicy, retrieval: DocumentRetrieval | None = None) -> None:
        """绑定访问边界、切片策略与可选的混合检索服务，缺少服务时明确降级。"""
        self.database, self.policy, self.retrieval = database, policy, retrieval

    def search(self, conversation_id: UUID, account_id: UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        """执行 BM25、语义、RRF 和重排；每次外部阶段后重核来源，返回完整正文。"""
        request = SearchDocumentsInput.model_validate(arguments)
        identifiers = [UUID(item) for item in request.document_ids] if request.document_ids is not None else None
        rows = self.search_rows(conversation_id, account_id, identifiers, validate_ids=True)
        candidates = max(30, self.policy.results)
        lexical = rank_chunks(rows, request.query, candidates)
        semantic: list[dict[str, Any]] = []
        degraded: list[str] = []
        def refresh(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """为每批外部嵌入及重排核验当前来源，排除期间撤回的切片。"""
            return self.live_candidates(candidates, conversation_id, account_id, identifiers)

        if rows:
            try:
                if self.retrieval is None:
                    raise ValueError('embedding_unavailable')
                semantic = self.retrieval.semantic(rows, request.query, candidates, refresh)
            except (ValueError, OSError, ModelFailure, httpx.HTTPError):
                degraded.append('embedding_unavailable')
        fused = reciprocal_rank_fusion([lexical, semantic], candidates)
        fused = self.live_candidates(fused, conversation_id, account_id, identifiers)
        reranked = False
        if fused:
            try:
                if self.retrieval is None:
                    raise ValueError('rerank_unavailable')
                fused = self.retrieval.rerank(fused, request.query)
                reranked = True
            except (ValueError, OSError, httpx.HTTPError) as error:
                degraded.append('rerank_input_too_long' if str(error) == 'rerank_input_too_long' else 'rerank_unavailable')
        results = self.live_candidates(fused, conversation_id, account_id, identifiers)[:self.policy.results]
        public = [{key: value for key, value in row.items() if key not in ('embedding', 'embedding_revision')} for row in results]
        return {'results': [{**row, 'document_id': str(row['document_id'])} for row in public], 'limit': self.policy.results,
                'retrieval': {'mode': 'hybrid' if semantic else 'bm25', 'bm25_candidates': len(lexical),
                              'vector_candidates': len(semantic), 'fused_candidates': len(fused),
                              'reranked': reranked, 'degraded': degraded}}

    def search_rows(self, conversation_id: UUID, account_id: UUID, identifiers: list[UUID] | None,
                    *, validate_ids: bool = False,
                    chunk_keys: list[tuple[str, int]] | None = None) -> list[dict[str, Any]]:
        """首次读取完整授权语料；网络阶段按切片身份精确核验，避免分批重复全量读取。"""
        chunk_documents = [UUID(key[0]) for key in chunk_keys] if chunk_keys is not None else None
        chunk_ordinals = [key[1] for key in chunk_keys] if chunk_keys is not None else None
        with self.database.connect() as connection:
            self.guard(connection, conversation_id, account_id)
            if validate_ids and identifiers is not None:
                valid = connection.execute(
                    "SELECT id FROM attachments WHERE conversation_id=%s AND id=ANY(%s) AND status='ready' "
                    "AND expires_at>CURRENT_TIMESTAMP AND knowledge_version=2", (conversation_id, identifiers),
                ).fetchall()
                if {item['id'] for item in valid} != set(identifiers):
                    raise LookupError('文档不存在、已过期或不可读取。')
            return connection.execute(
                "SELECT d.attachment_id AS document_id,a.filename,d.ordinal AS chunk_id,d.content,"
                "d.embedding,d.embedding_revision,"
                "u.page,u.paragraph,e.page AS end_page,e.paragraph AS end_paragraph,d.start_offset,d.end_offset FROM document_chunks d "
                "JOIN document_units u ON u.attachment_id=d.attachment_id AND u.ordinal=d.unit_ordinal "
                "JOIN document_units e ON e.attachment_id=d.attachment_id AND e.ordinal=d.end_unit_ordinal "
                "JOIN attachments a ON a.id=d.attachment_id WHERE a.conversation_id=%s AND a.status='ready' "
                "AND a.expires_at>CURRENT_TIMESTAMP AND a.knowledge_version=2 "
                "AND (%s::uuid[] IS NULL OR a.id=ANY(%s)) "
                "AND (%s::uuid[] IS NULL OR (d.attachment_id,d.ordinal) IN "
                "(SELECT * FROM unnest(%s::uuid[],%s::int[]))) ORDER BY a.created_at,a.id,d.ordinal",
                (conversation_id, identifiers, identifiers, chunk_documents, chunk_documents, chunk_ordinals),
            ).fetchall()

    def live_candidates(self, candidates: list[dict[str, Any]], conversation_id: UUID, account_id: UUID,
                        identifiers: list[UUID] | None) -> list[dict[str, Any]]:
        """网络阶段结束后重新核验内容和原文位置，阻止删除、到期和重切片的迟到结果。"""
        current = {chunk_key(row): row for row in self.search_rows(
            conversation_id, account_id, identifiers, chunk_keys=[chunk_key(row) for row in candidates])}
        valid: list[dict[str, Any]] = []
        fields = ('content', 'page', 'paragraph', 'end_page', 'end_paragraph', 'start_offset', 'end_offset')
        for row in candidates:
            live = current.get(chunk_key(row))
            if live is not None and all(live[field] == row[field] for field in fields):
                valid.append(live)
        return valid

    def read(self, conversation_id: UUID, account_id: UUID, arguments: dict[str, Any],
             counter: TokenCounter, available_tokens: int) -> dict[str, Any]:
        """按第二版自然段原文连续读取，完整结果受单次上限和当前上下文预算约束。"""
        request = ReadDocumentInput.model_validate(arguments)
        identifier = UUID(request.document_id)
        with self.database.connect() as connection:
            self.guard(connection, conversation_id, account_id)
            document = connection.execute(
                "SELECT filename FROM attachments WHERE id=%s AND conversation_id=%s AND status='ready' "
                "AND expires_at>CURRENT_TIMESTAMP AND knowledge_version=2",
                (identifier, conversation_id),
            ).fetchone()
            if document is None:
                raise LookupError('文档不存在、已过期或不可读取。')
            units = connection.execute('SELECT ordinal,page,paragraph,content FROM document_units WHERE attachment_id=%s ORDER BY ordinal', (identifier,)).fetchall()
        paginated = any(unit['page'] is not None for unit in units)
        if paginated and (request.start_paragraph is not None or request.end_paragraph is not None):
            raise ValueError('PDF／Word 请使用页码范围。')
        if not paginated and (request.start_page is not None or request.end_page is not None):
            raise ValueError('非分页文档请使用段落范围。')
        key = 'page' if paginated else 'paragraph'
        maximum = max((unit[key] for unit in units), default=0)
        start = (request.start_page if paginated else request.start_paragraph) or 1
        end = (request.end_page if paginated else request.end_paragraph) or maximum
        if not 1 <= start <= end <= maximum:
            raise ValueError('读取范围超出文档范围。')
        selected = [unit for unit in units if start <= unit[key] <= end]
        if request.cursor is not None:
            selected = [unit for unit in selected if unit['ordinal'] >= request.cursor.unit]
            if not selected or selected[0]['ordinal'] != request.cursor.unit or request.cursor.offset > len(selected[0]['content']):
                raise ValueError('续读位置不属于当前读取范围。')
        # 计入图执行器追加的调用计数字段，保证最终工具结果也在上限内。
        budget = max(0, min(10000, available_tokens) - 128)
        return self.read_result(request, document['filename'], selected, counter, budget)

    @staticmethod
    def guard(connection: Any, conversation_id: UUID, account_id: UUID) -> None:
        """在工具查询前核对账号和未删除会话，防止伪造文档标识跨会话读取。"""
        if connection.execute('SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting',
                              (conversation_id, account_id)).fetchone() is None:
            raise LookupError('会话不存在。')

    @staticmethod
    def read_result(request: ReadDocumentInput, filename: str, units: list[dict[str, Any]],
                    counter: TokenCounter, budget: int) -> dict[str, Any]:
        """构造预算内的原文块和无损游标，连结果包装一起计数，不足时返回明确错误。"""
        result: dict[str, Any] = {'document_id': request.document_id, 'filename': filename, 'content': [],
                                  'complete': True, 'next_cursor': None, 'token_count_method': counter.method}
        blocks: list[dict[str, Any]] = result['content']
        for index, unit in enumerate(units):
            offset = request.cursor.offset if index == 0 and request.cursor is not None else 0
            remaining = unit['content'][offset:]
            block = {'page': unit['page'], 'paragraph': unit['paragraph'], 'start_offset': offset,
                     'end_offset': offset, 'content': ''}
            blocks.append(block)
            # 先按最坏续读包装保留位置，避免正文填满后游标挤出预算。
            result['complete'] = False
            result['next_cursor'] = {'unit': unit['ordinal'], 'offset': len(unit['content'])}
            overhead = counter.count(json.dumps(result, ensure_ascii=False))
            text = counter.prefix(remaining, max(0, budget - overhead))
            block.update(content=text, end_offset=offset + len(text))
            result['next_cursor'] = {'unit': unit['ordinal'], 'offset': offset + len(text)}
            # JSON 转义和分词边界会改变计数，必须对完整结果再次核验。
            while text and counter.count(json.dumps(result, ensure_ascii=False)) > budget:
                text = text[:max(0, len(text) - max(1, len(text) // 20))]
                block.update(content=text, end_offset=offset + len(text))
                result['next_cursor']['offset'] = offset + len(text)
            if len(text) < len(remaining):
                if not text:
                    blocks.pop()
                if not blocks:
                    return {'error': '当前上下文预算不足以返回原文，请先释放上下文空间。', 'code': 'context_budget_exhausted'}
                return result
            result['complete'] = index == len(units) - 1
            result['next_cursor'] = None if result['complete'] else {'unit': units[index + 1]['ordinal'], 'offset': 0}
            if counter.count(json.dumps(result, ensure_ascii=False)) > budget:
                blocks.pop()
                result['complete'] = False
                result['next_cursor'] = {'unit': unit['ordinal'], 'offset': offset}
                if not blocks:
                    return {'error': '当前上下文预算不足以返回原文，请先释放上下文空间。', 'code': 'context_budget_exhausted'}
                return result
        return result
