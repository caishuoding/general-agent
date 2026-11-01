"""将当前用户授权与回答材料分开，供同步和流式入口共用。"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID
from typing import Any, cast

from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.calls import CallGateway
from general_agent.memory.service import MemoryService
from general_agent.memory.deletion import MemoryDeletion
from general_agent.memory.correction import MemoryCorrection
from general_agent.memory.recall import MemoryRecall
from general_agent.infrastructure.provider import ModelMessage
from general_agent.infrastructure.settings import StoredSettings
from general_agent.attachments.service import AttachmentContext
from general_agent.search.service import SearchService
from general_agent.memory.automatic import AutomaticMemory
from general_agent.memory.tools import MemoryTools
from general_agent.knowledge.tools import KnowledgeTools
from general_agent.agent.graph import TurnFinalizer, TurnPreparer
from general_agent.agent.runner import AgentRuntime
from general_agent.conversation.progress import MessageProgress
from general_agent.prompts import (
    base_system_prompt,
)


@dataclass(frozen=True)
class AnswerPreparation:
    """统一回答材料和本地可复用结果，传输入口只负责发送与持久化各自的答案。"""
    messages: list[ModelMessage]
    material: AttachmentContext


class ChatExecution:
    """共用工具、来源管理和回答准备；传输、线程及答案尝试留给聊天入口。"""

    def __init__(self, calls: CallGateway, memory: MemoryService, search: SearchService,
                 knowledge: KnowledgeTools | None = None) -> None:
        """绑定自动记忆和各资料工具，所有聊天入口共用同一执行流程。"""
        self.calls, self.memory = calls, memory
        self.recall = MemoryRecall(memory)
        self.search = search
        self.correction = MemoryCorrection(memory)
        self.deletion = MemoryDeletion(memory)
        self.automatic = AutomaticMemory(memory)
        self.memory_tools = MemoryTools(memory, self.automatic, search, search.attachments, knowledge)
        self.knowledge = knowledge
        self.agent_runtime = AgentRuntime(self.memory_tools)

    def embedding_revision(self) -> str | None:
        """返回当前嵌入配置版本；Agent 每轮都可能选择记忆工具，因此每轮都绑定版本。"""
        embedding = self.memory.embedding.store.read()
        return embedding.revision if embedding else None

    def scope(self, content: str, settings: StoredSettings) -> list[str | None]:
        """返回聊天、嵌入及搜索配置版本组成的幂等范围，防止操作结果跨配置复用。"""
        search = self.search.settings.read()
        return [content, settings.revision, self.embedding_revision(), search.revision if search else None]

    def prepare_answer(self, user_id: UUID, content: str, operation: OperationContext) -> list[ModelMessage]:
        """提供正式相关记忆，回答只能根据实际状态声称已记住。"""
        return self.recall.prepare(user_id, content, operation)

    def prepare_response(
        self,
        conversation_id: UUID,
        account_id: UUID,
        user_id: UUID,
        operation: OperationContext,
        rows: list[dict[str, Any]],
    ) -> AnswerPreparation:
        """刷新记忆与附件并合入工作消息，旧临时材料被替换且当前请求保持固定。"""
        users = [row for row in rows if row.get('role') == 'user']
        current = next((row for row in users if row.get('_current_request')), users[-1])
        content = str(current['content'])
        material = self.search.attachments.prepare(conversation_id, account_id)
        memory_material = self.prepare_answer(user_id, content, operation)
        system_prompt: ModelMessage = {
            'role': 'system',
            'content': base_system_prompt(),
        }
        data_messages: list[ModelMessage] = [cast(ModelMessage, {**message, '_context_material': True}) for message in [*memory_material, *material.messages]]
        conversation: list[ModelMessage] = [system_prompt, *data_messages]
        for index, row in enumerate(rows):
            if row.get('role') == 'system' or row.get('_context_material'):
                continue
            message = {key: value for key, value in row.items() if key not in ('id', 'position', 'status', 'skill_names')}
            message.pop('_current_request', None)
            if row is current:
                message['_current_request'] = True
            if row.get('id') and not message.get('_message_id'):
                message['_message_id'] = str(row['id'])
            conversation.append(cast(ModelMessage, message))
        return AnswerPreparation(conversation, material)

    def run_answer(
        self,
        conversation_id: UUID,
        account_id: UUID,
        user_id: UUID,
        operation: OperationContext,
        settings: StoredSettings,
        *,
        messages: list[ModelMessage] | None,
        assistant_message_id: UUID,
        on_content: Callable[[str], None] | None = None,
        on_progress: Callable[[MessageProgress], None] | None = None,
        on_round_start: Callable[[], None] | None = None,
        prepare_turn: TurnPreparer | None = None,
        finalize_turn: TurnFinalizer | None = None,
        model_failure: Callable[[Exception, str], dict[str, Any]] | None = None,
        initial_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """运行完整回答图；主流程提供首轮准备和业务提交，子执行复用同一循环。"""
        def default_prepare(_state: Any) -> dict[str, Any]:
            """未提供主流程准备时保持当前工作消息不变。"""
            return {}

        def default_finalize(state: Any) -> dict[str, Any]:
            """未提供业务收尾时确认共享循环可以结束。"""
            return {**state, 'settled': True}

        return self.agent_runtime.run_answer(conversation_id, account_id, user_id, assistant_message_id,
            settings, operation, messages, on_content, on_progress, on_round_start,
            prepare_turn or default_prepare, finalize_turn or default_finalize,
            model_failure, initial_state or {})

    def close_agent_execution(self, answer_id: UUID) -> None:
        """幂等关闭根 Agent 及遗留子任务，供回答最终业务提交后调用。"""
        self.agent_runtime.repository.close(answer_id)

    @staticmethod
    def material_attachment_ids(material: AttachmentContext | None) -> list[UUID]:
        """从文档清单中提取稳定附件标识，供执行判断是否开放会话文档工具。"""
        identifiers: list[UUID] = []
        for message in material.messages if material is not None else []:
            content = message.get("content")
            if not isinstance(content, str) or '"document_id"' not in content:
                continue
            try:
                rows = json.loads(content)
            except ValueError:
                continue
            if not isinstance(rows, list):
                continue
            identifiers.extend(
                UUID(str(item["document_id"]))
                for item in rows
                if isinstance(item, dict) and item.get("document_id")
            )
        return list(dict.fromkeys(identifiers))

    def resolve_knowledge_citations(self, sources: list[dict[str, Any]], answer: str) -> tuple[str, list[dict[str, Any]]]:
        """用主执行取得或子任务已交付的来源核对 [K编号]，返回实际引用。"""
        if self.knowledge is None:
            return answer, []
        return self.knowledge.resolve_citations(sources, answer)

    def memory_deletion_invalidates_checkpoint(self, user_message_id: UUID) -> bool:
        """判断本轮是否已停用观察到的记忆，以便终态清除派生会话检查点。"""
        with self.memory.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM memory_tool_observations o "
                "JOIN memory_actions a ON a.id=o.action_id "
                "WHERE o.message_id=%s AND a.status IN ('deleting','deleted') LIMIT 1",
                (user_message_id,),
            ).fetchone()
        return row is not None
