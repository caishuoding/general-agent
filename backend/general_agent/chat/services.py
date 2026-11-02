"""聚合聊天业务依赖，不负责构建或运行 Agent 图。"""

from typing import Any, cast
from uuid import UUID

from general_agent.agent.state import AttachmentAccess, KnowledgeBaseAccess
from general_agent.attachments.service import AttachmentService
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelMessage
from general_agent.infrastructure.settings import StoredSettings
from general_agent.knowledge.tools import KnowledgeTools
from general_agent.memory.automatic import AutomaticMemory
from general_agent.memory.correction import MemoryCorrection
from general_agent.memory.deletion import MemoryDeletion
from general_agent.memory.recall import MemoryRecall
from general_agent.memory.service import MemoryService
from general_agent.memory.tools import MemoryTools
from general_agent.search.service import SearchService


class ChatServices:
    """提供记忆、搜索、附件、知识库和工具服务，不持有图运行状态。"""

    def __init__(self, memory: MemoryService, search: SearchService,
                 knowledge: KnowledgeTools | None = None) -> None:
        """绑定聊天业务服务并创建唯一的会话级工具集合。"""
        self.memory, self.search = memory, search
        self.attachments: AttachmentService = search.attachments
        self.knowledge = knowledge
        self.recall = MemoryRecall(memory)
        self.correction = MemoryCorrection(memory)
        self.deletion = MemoryDeletion(memory)
        self.automatic = AutomaticMemory(memory)
        self.tools = MemoryTools(memory, self.automatic, search, self.attachments, knowledge)

    def embedding_revision(self) -> str | None:
        """返回当前嵌入配置版本，供操作幂等范围使用。"""
        embedding = self.memory.embedding.store.read()
        return embedding.revision if embedding else None

    def scope(self, content: str, settings: StoredSettings) -> list[str | None]:
        """返回聊天、嵌入及搜索配置版本组成的幂等范围。"""
        search = self.search.settings.read()
        return [content, settings.revision, self.embedding_revision(), search.revision if search else None]

    def prepare_response(
        self,
        conversation_id: UUID,
        account_id: UUID,
        user_id: UUID,
        operation: OperationContext,
        rows: list[dict[str, Any]],
    ) -> list[ModelMessage]:
        """刷新记忆与附件并合入工作消息，旧临时材料被替换且当前请求保持固定。"""
        users = [row for row in rows if row.get('role') == 'user' and not row.get('_context_material')]
        current = next((row for row in users if str(row.get('id')) == str(user_id)), users[-1])
        content = str(current['content'])
        material = self.attachments.prepare(conversation_id, account_id)
        memory_material = self.recall.prepare(user_id, content, operation)
        memory_messages: list[ModelMessage] = [
            cast(ModelMessage, {**message, '_context_material': True})
            for message in memory_material
        ]
        attachment_messages: list[ModelMessage] = [
            cast(ModelMessage, {**message, '_context_material': True, '_context_pinned': True})
            for message in material.messages
        ]
        data_messages: list[ModelMessage] = [*memory_messages, *attachment_messages]
        conversation: list[ModelMessage] = [*data_messages]
        for row in rows:
            if row.get('role') == 'system' or row.get('_context_material'):
                continue
            message = {key: value for key, value in row.items() if key not in ('id', 'position', 'status', 'skill_names')}
            message.pop('_current_request', None)
            if row is current:
                message['_current_request'] = True
            if row.get('id'):
                message['id'] = str(row['id'])
            conversation.append(cast(ModelMessage, message))
        return conversation

    def attachment_access(self, conversation_id: UUID) -> list[AttachmentAccess]:
        """返回当前会话仍有效附件的公开身份和文件名。"""
        with self.memory.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,filename,image_metadata,knowledge_version FROM attachments WHERE conversation_id=%s "
                "AND status='ready' AND expires_at>CURRENT_TIMESTAMP ORDER BY created_at,id",
                (conversation_id,),
            ).fetchall()
        return [{
            'id': str(row['id']), 'filename': str(row['filename']),
            'kind': 'image' if row['image_metadata'] is not None else (
                'document' if row['knowledge_version'] == 2 else 'source'),
        } for row in rows]

    def knowledge_base_access(self, conversation_id: UUID) -> list[KnowledgeBaseAccess]:
        """返回当前会话已绑定且未删除的知识库身份和名称。"""
        with self.memory.database.connect() as connection:
            rows = connection.execute(
                'SELECT b.id,b.name FROM conversation_knowledge_bases s '
                'JOIN knowledge_bases b ON b.id=s.knowledge_base_id '
                'WHERE s.conversation_id=%s AND NOT b.deleting ORDER BY b.created_at,b.id',
                (conversation_id,),
            ).fetchall()
        return [{'id': str(row['id']), 'name': str(row['name'])} for row in rows]

    def resolve_knowledge_citations(self, sources: list[dict[str, Any]], answer: str) -> tuple[str, list[dict[str, Any]]]:
        """用主执行取得或子任务已交付的来源核对知识库编号。"""
        if self.knowledge is None:
            return answer, []
        return self.knowledge.resolve_citations(sources, answer)

    def memory_deletion_invalidates_checkpoint(self, user_message_id: UUID) -> bool:
        """判断本轮是否已停用观察到的记忆，以便终态清除派生检查点。"""
        with self.memory.database.connect() as connection:
            row = connection.execute(
                'SELECT 1 FROM memory_tool_observations o '
                'JOIN memory_actions a ON a.id=o.action_id '
                "WHERE o.message_id=%s AND a.status IN ('deleting','deleted') LIMIT 1",
                (user_message_id,),
            ).fetchone()
        return row is not None
