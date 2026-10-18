"""只在当前明确要求时检索其他会话消息，不读附件或个人记忆集合。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from general_agent.infrastructure.provider import ModelMessage
from general_agent.prompts import history_material

if TYPE_CHECKING:
    from general_agent.infrastructure.operations import OperationContext
    from general_agent.infrastructure.database import Database


LIMIT = 8
EXCERPT_LIMIT = 1200


def history_query(content: str) -> str | None:
    """识别整条当前消息的明确历史查询；引用、否定或解释中的句子不是授权。"""
    match = re.fullmatch(r'\s*(?:请)?(?:帮我)?(?:查找|搜索|查询|检索|翻查|回看|查)(?:一下)?\s*(?:其他会话的|以前的|之前的|过去的)?(?:旧聊天|历史聊天|旧消息|聊天记录|之前的聊天)(?:\s*[：:]\s*([^\n]*)|\s+([^\n]+))?', content.strip())
    return (match.group(1) or match.group(2) or '').strip() if match else None


def query_terms(query: str) -> list[str]:
    """提取有限的原文关键词；不生成云端查询或语义向量。"""
    return list(dict.fromkeys(re.findall(r'[\w\u4e00-\u9fff]{2,80}', query.lower())))[:12]


def content_hash(content: str) -> str:
    """用正文指纹检测消息已修改，引用失效后不能继续显示旧摘录。"""
    return hashlib.sha256(content.encode()).hexdigest()


def excerpt(content: str, terms: list[str]) -> str:
    """截取首个匹配词附近的有限原文，明确标出被截掉的上下文。"""
    positions = [content.lower().find(term) for term in terms if term in content.lower()]
    start = max(0, min(positions, default=0) - 200)
    end = min(len(content), start + EXCERPT_LIMIT)
    return ('…' if start else '') + content[start:end] + ('…' if end < len(content) else '')


def public_history(database: 'Database', operation_id: UUID | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """只从未停用会话的有效原消息重建出处，删除过程即使尚未物理清除也不公开摘录。"""
    if operation_id is None:
        return None, []
    with database.connect() as connection:
        query = connection.execute('SELECT h.*,m.content FROM history_queries h JOIN messages m ON m.id=h.request_message_id WHERE h.operation_id=%s', (operation_id,)).fetchone()
        if not query:
            return None, []
        rows = connection.execute("SELECT h.*,m.content,m.role,m.created_at,c.title FROM history_hits h JOIN messages m ON m.id=h.source_message_id AND m.conversation_id=h.source_conversation_id JOIN conversations c ON c.id=m.conversation_id WHERE h.operation_id=%s AND m.status='complete' AND NOT c.deleting ORDER BY h.ordinal", (operation_id,)).fetchall()
    terms = query_terms(str(query.get('query') or history_query(query['content']) or ''))
    sources = [{'key': row['source_key'], 'conversation_id': str(row['source_conversation_id']), 'conversation_title': row['title'], 'message_id': str(row['source_message_id']), 'role': row['role'], 'created_at': row['created_at'].isoformat(), 'excerpt': excerpt(row['content'], terms)} for row in rows if content_hash(row['content']) == row['source_hash']]
    lost = len(sources) != query['result_count']
    status = 'unavailable' if lost else ('complete' if sources else 'empty')
    detail = '部分或全部历史来源已不可用，请重新查询。' if lost else (f'已在其他会话中找到 {len(sources)} 条消息；仅查询消息正文，未读取旧附件。' if sources else '未找到匹配的其他会话消息。可使用更短的原文关键词重试。')
    return {'status': status, 'count': len(sources), 'detail': detail}, sources


@dataclass(frozen=True)
class HistoryContext:
    """保存单次历史回答的有据材料；正文仅在本次执行内存中存在。"""
    messages: list[ModelMessage]
    local_answer: str | None = None


class HistorySearch:
    """本机独立词法检索边界，只选择有效消息并持久化无正文出处关系。"""

    def __init__(self, database: 'Database') -> None:
        """绑定消息数据库，不接收附件、Mem0 或任何外部检索客户端。"""
        self.database = database

    def prepare(self, conversation_id: UUID, user_id: UUID, content: str, operation: OperationContext) -> HistoryContext | None:
        """明确查询才检索；登记时锁有效来源会话，恢复重核来源并不重放已停用正文。"""
        query = history_query(content)
        if query is None:
            return None
        terms = query_terms(query)
        if not terms:
            return HistoryContext([], '请在“查找旧聊天：”后填写至少两个字符的原文关键词。')
        with self.database.connect() as connection:
            original = connection.execute("SELECT id FROM messages WHERE id=%s AND conversation_id=%s AND role='user' AND content=%s", (user_id, conversation_id, content)).fetchone()
            if not original:
                raise ValueError('历史查询的当前用户来源已不可用。')
            previous = connection.execute('SELECT operation_id FROM history_queries WHERE operation_id=%s', (operation.operation_id,)).fetchone()
            if not previous:
                patterns = ['%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%' for term in terms]
                rows = connection.execute("SELECT m.id,m.conversation_id,m.position,m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.conversation_id<>%s AND m.status='complete' AND NOT c.deleting AND m.content ILIKE ANY(%s) ORDER BY m.position DESC LIMIT %s FOR SHARE OF c", (conversation_id, patterns, LIMIT)).fetchall()
                connection.execute('INSERT INTO history_queries(operation_id,conversation_id,request_message_id,result_count,query) VALUES(%s,%s,%s,%s,%s)', (operation.operation_id, conversation_id, user_id, len(rows), query))
                for ordinal, row in enumerate(rows):
                    connection.execute('INSERT INTO history_hits(operation_id,source_message_id,source_conversation_id,source_hash,source_key,ordinal) VALUES(%s,%s,%s,%s,%s,%s)', (operation.operation_id, row['id'], row['conversation_id'], content_hash(row['content']), f'H{row["position"]}', ordinal))
        state, sources = public_history(self.database, operation.operation_id)
        assert state is not None
        if state['status'] != 'complete':
            return HistoryContext([], state['detail'])
        material: list[ModelMessage] = [{'role': 'system', 'content': item} for item in history_material(sources)]
        return HistoryContext(material)

    def tool(self, conversation_id: UUID, user_id: UUID, query: str, operation: OperationContext) -> dict[str, Any]:
        """在模型选择工具后查询其他会话正文；返回状态和带出处的有限结果，不读取旧附件。"""
        terms = query_terms(query)
        if not terms:
            raise ValueError('历史聊天查询至少需要一个两个字符的关键词。')
        with self.database.connect() as connection:
            original = connection.execute("SELECT id FROM messages WHERE id=%s AND conversation_id=%s AND role='user'", (user_id, conversation_id)).fetchone()
            if original is None:
                raise ValueError('历史查询的当前用户来源已不可用。')
            previous = connection.execute('SELECT operation_id FROM history_queries WHERE operation_id=%s', (operation.operation_id,)).fetchone()
            if previous is None:
                patterns = ['%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%' for term in terms]
                rows = connection.execute("SELECT m.id,m.conversation_id,m.position,m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.conversation_id<>%s AND m.status='complete' AND NOT c.deleting AND m.content ILIKE ANY(%s) ORDER BY m.position DESC LIMIT %s FOR SHARE OF c", (conversation_id, patterns, LIMIT)).fetchall()
                connection.execute('INSERT INTO history_queries(operation_id,conversation_id,request_message_id,result_count,query) VALUES(%s,%s,%s,%s,%s)', (operation.operation_id, conversation_id, user_id, len(rows), query))
                for ordinal, row in enumerate(rows):
                    connection.execute('INSERT INTO history_hits(operation_id,source_message_id,source_conversation_id,source_hash,source_key,ordinal) VALUES(%s,%s,%s,%s,%s,%s)', (operation.operation_id, row['id'], row['conversation_id'], content_hash(row['content']), f'H{row["position"]}', ordinal))
        state, sources = public_history(self.database, operation.operation_id)
        assert state is not None
        return {'status': state['status'], 'detail': state['detail'], 'results': sources}

    def available(self, operation: OperationContext) -> bool:
        """输出每段云端结果前重新确认全部原消息仍有效，来源删除不能被迟到回答绕过。"""
        state, _ = public_history(self.database, operation.operation_id)
        return state is not None and state['status'] == 'complete'

    def resolve(self, operation: OperationContext, answer: str, required: bool = False) -> str:
        """只接受本次实际来源标记，空结果和失效来源使用真实本地回执。"""
        state, sources = public_history(self.database, operation.operation_id)
        if state is None:
            return '历史来源已不可用，请重新查询。' if required else answer
        if state['status'] != 'complete':
            return str(state['detail'])
        allowed = {source['key'] for source in sources}

        def replace(match: re.Match[str]) -> str:
            """将模型杜撰的历史引用替换成明确的未核验提示。"""
            return match.group(0) if match.group(1) in allowed else '[历史出处未核验]'

        result = re.sub(r'\[(H\d+)\]', replace, answer)
        if not any(f'[{key}]' in result for key in allowed):
            result += '\n\n回答缺少历史消息引用；请核对下方实际检索到的消息。'
        return result
