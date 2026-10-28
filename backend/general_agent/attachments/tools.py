"""提供仅限当前会话的文档检索及按页顺序读取共用切片的工具。"""

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from general_agent.prompts.tools import TOOL_DESCRIPTIONS

from general_agent.attachments.knowledge import ChunkPolicy
from general_agent.attachments.retrieval import DocumentRetrieval, chunk_key, hybrid_retrieve
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.tokens import TokenCounter


class SearchDocumentsInput(BaseModel):
    """查询可限定当前会话内的文档，返回数量由后台切片策略决定。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=2000)
    document_ids: list[str] | None = Field(default=None, max_length=50)


class ReadCursor(BaseModel):
    """定位下一片待返回切片；仅由工具生成，续读时沿用原页码或段落范围。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    chunk_id: int = Field(ge=1)


class ReadDocumentInput(BaseModel):
    """分页文档按页定位切片，非分页文档按段落定位；省略范围顺序读取全部切片。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    document_id: str
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    start_paragraph: int | None = Field(default=None, ge=1)
    end_paragraph: int | None = Field(default=None, ge=1)
    cursor: ReadCursor | None = None


def document_tools() -> list[dict[str, Any]]:
    """返回会话文档检索和读取工具的模型定义；无需输入，不在此执行文档操作。"""
    definitions: list[tuple[str, str, type[BaseModel]]] = [
        ('search_documents', TOOL_DESCRIPTIONS['search_documents'], SearchDocumentsInput),
        ('read_document', TOOL_DESCRIPTIONS['read_document'], ReadDocumentInput),
    ]
    return [{'type': 'function', 'function': {'name': name, 'description': description,
             'parameters': schema.model_json_schema()}} for name, description, schema in definitions]


class DocumentTools:
    """隐藏文档访问权限、切片召回和原文位置，不接受模型指定会话身份。"""

    def __init__(self, database: Database, policy: ChunkPolicy, retrieval: DocumentRetrieval | None = None) -> None:
        """绑定访问边界、切片策略与可选的混合检索服务，缺少服务时明确降级。"""
        self.database, self.policy, self.retrieval = database, policy, retrieval

    def search(self, conversation_id: UUID, account_id: UUID, arguments: dict[str, Any]) -> dict[str, Any]:
        """通过共用混合算法召回附件；授权与实时来源检查仍只访问会话附件表。"""
        request = SearchDocumentsInput.model_validate(arguments)
        identifiers = [UUID(item) for item in request.document_ids] if request.document_ids is not None else None
        rows = self.search_rows(conversation_id, account_id, identifiers, validate_ids=True)
        def refresh(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """为每批外部嵌入及重排核验当前来源，排除期间撤回的切片。"""
            return self.live_candidates(candidates, conversation_id, account_id, identifiers)

        def semantic(candidates: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
            """调用附件的按需向量缓存路径，不把此生命周期带入知识库。"""
            if self.retrieval is None:
                raise ValueError('embedding_unavailable')
            return self.retrieval.semantic(candidates, query, limit, refresh)

        def rerank(candidates: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
            """复用重排协议，未配置时由公共流程标记降级。"""
            if self.retrieval is None:
                raise ValueError('rerank_unavailable')
            return self.retrieval.rerank(candidates, query)

        return hybrid_retrieve(rows, request.query, self.policy.results, semantic, rerank, refresh)

    def search_rows(self, conversation_id: UUID, account_id: UUID, identifiers: list[UUID] | None,
                    *, validate_ids: bool = False,
                    chunk_keys: list[tuple[str, int]] | None = None) -> list[dict[str, Any]]:
        """直接读取授权切片及自带位置、结构和计数元数据，无需关联另一份原文。"""
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
                "d.context_prefix,d.context_suffix,d.token_count,d.token_count_method,"
                "d.page,d.paragraph,d.end_page,d.end_paragraph,d.structure,d.end_structure,d.image_id "
                "FROM document_chunks d "
                "JOIN attachments a ON a.id=d.attachment_id WHERE a.conversation_id=%s AND a.status='ready' "
                "AND a.expires_at>CURRENT_TIMESTAMP AND a.knowledge_version=2 "
                "AND (%s::uuid[] IS NULL OR a.id=ANY(%s)) "
                "AND (%s::uuid[] IS NULL OR (d.attachment_id,d.ordinal) IN "
                "(SELECT * FROM unnest(%s::uuid[],%s::int[]))) ORDER BY a.created_at,a.id,d.ordinal",
                (conversation_id, identifiers, identifiers, chunk_documents, chunk_documents, chunk_ordinals),
            ).fetchall()

    def live_candidates(self, candidates: list[dict[str, Any]], conversation_id: UUID, account_id: UUID,
                        identifiers: list[UUID] | None) -> list[dict[str, Any]]:
        """网络阶段后核验正文、结构和页码，阻止删除、到期和 Word 重切片的迟到结果。"""
        current = {chunk_key(row): row for row in self.search_rows(
            conversation_id, account_id, identifiers, chunk_keys=[chunk_key(row) for row in candidates])}
        valid: list[dict[str, Any]] = []
        fields = ('content', 'page', 'paragraph', 'end_page', 'end_paragraph',
                  'structure', 'end_structure', 'context_prefix', 'context_suffix', 'token_count',
                  'token_count_method', 'image_id')
        for row in candidates:
            live = current.get(chunk_key(row))
            if live is not None and all(live[field] == row[field] for field in fields):
                valid.append(live)
        return valid

    def read(self, conversation_id: UUID, account_id: UUID, arguments: dict[str, Any],
             counter: TokenCounter, available_tokens: int) -> dict[str, Any]:
        """按页或段落元数据筛选共用切片，按原文顺序整片返回，不裁剪跨页和重叠内容。"""
        request = ReadDocumentInput.model_validate(arguments)
        identifier = UUID(request.document_id)
        with self.database.connect() as connection:
            self.guard(connection, conversation_id, account_id)
            document = connection.execute(
                "SELECT filename,parse_info FROM attachments WHERE id=%s AND conversation_id=%s AND status='ready' "
                "AND expires_at>CURRENT_TIMESTAMP AND knowledge_version=2",
                (identifier, conversation_id),
            ).fetchone()
            if document is None:
                raise LookupError('文档不存在、已过期或不可读取。')
            paginated = document['parse_info'].get('format') in ('pdf', 'docx')
            if paginated and (request.start_paragraph is not None or request.end_paragraph is not None):
                raise ValueError('PDF／Word 请使用页码范围。')
            if not paginated and (request.start_page is not None or request.end_page is not None):
                raise ValueError('非分页文档请使用段落范围。')
            maximum = document['parse_info'].get('total_units', 0)
            start = (request.start_page if paginated else request.start_paragraph) or 1
            end = (request.end_page if paginated else request.end_paragraph) or maximum
            if not 1 <= start <= end <= maximum:
                raise ValueError('读取范围超出文档范围。')
            # 列名由文件格式白名单选择；范围和游标仍使用绑定参数。
            key = 'page' if paginated else 'paragraph'
            chunks = connection.execute(
                f'SELECT ordinal,content,image_id,structure FROM document_chunks WHERE attachment_id=%s '
                f'AND {key}<=%s AND end_{key}>=%s AND ordinal>=%s ORDER BY ordinal',
                (identifier, end, start, request.cursor.chunk_id if request.cursor else 1),
            ).fetchall()
        if request.cursor is not None and (not chunks or chunks[0]['ordinal'] != request.cursor.chunk_id):
            raise ValueError('续读位置不属于当前读取范围。')
        # 计入图执行器追加的调用计数字段，保证最终工具结果也在上限内。
        budget = max(0, min(10000, available_tokens) - 128)
        return self.read_result(request, document['filename'], chunks, counter, budget)

    @staticmethod
    def guard(connection: Any, conversation_id: UUID, account_id: UUID) -> None:
        """在工具查询前核对账号和未删除会话，防止伪造文档标识跨会话读取。"""
        if connection.execute('SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting',
                              (conversation_id, account_id)).fetchone() is None:
            raise LookupError('会话不存在。')

    @staticmethod
    def read_result(request: ReadDocumentInput, filename: str, chunks: list[dict[str, Any]],
                    counter: TokenCounter, budget: int) -> dict[str, Any]:
        """整片装入预算，连包装一起计数；剩余切片用游标续读，不拆正文或移除重叠。"""
        result: dict[str, Any] = {'document_id': request.document_id, 'filename': filename, 'content': [],
                                  'complete': True, 'next_cursor': None, 'token_count_method': counter.method}
        blocks: list[dict[str, Any]] = result['content']
        for index, chunk in enumerate(chunks):
            structure = chunk.get('structure')
            blocks.append({
                'chunk_id': chunk['ordinal'],
                'content': chunk['content'],
                **({'image_id': str(chunk['image_id'])} if chunk.get('image_id') else {}),
                **({'structure': structure} if isinstance(structure, dict) and structure.get('table') else {}),
            })
            result['complete'] = index == len(chunks) - 1
            result['next_cursor'] = None if result['complete'] else {'chunk_id': chunks[index + 1]['ordinal']}
            if counter.count(json.dumps(result, ensure_ascii=False)) > budget:
                blocks.pop()
                result['complete'] = False
                result['next_cursor'] = {'chunk_id': chunk['ordinal']}
                # 末片由 complete 改为游标会增大包装，必要时再退回上一整片。
                while blocks and counter.count(json.dumps(result, ensure_ascii=False)) > budget:
                    removed = blocks.pop()
                    result['next_cursor'] = {'chunk_id': removed['chunk_id']}
                if not blocks:
                    return {'error': '当前上下文预算不足以返回完整切片，请先释放上下文空间。', 'code': 'context_budget_exhausted'}
                return result
        if counter.count(json.dumps(result, ensure_ascii=False)) > budget:
            return {'error': '当前上下文预算不足以返回完整切片，请先释放上下文空间。', 'code': 'context_budget_exhausted'}
        return result
