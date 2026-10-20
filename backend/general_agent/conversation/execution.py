"""将当前用户授权与回答材料分开，供同步和流式入口共用。"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID
from typing import Any

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
from general_agent.memory.tools import MemoryTools, ToolOutcome
from general_agent.prompts import (
    attachment_context_fragment,
    compose_system_prompt,
    memory_context_fragment,
)


@dataclass(frozen=True)
class AnswerPreparation:
    """统一回答材料和本地可复用结果，传输入口只负责发送与持久化各自的答案。"""
    messages: list[ModelMessage]
    material: AttachmentContext
    reserved: UUID | None


class ChatExecution:
    """共用工具、来源管理和回答准备；传输、线程及答案尝试留给聊天入口。"""

    def __init__(self, calls: CallGateway, memory: MemoryService, search: SearchService) -> None:
        """绑定自动记忆、工具和材料路由，所有聊天入口共用同一执行流程。"""
        self.calls, self.memory = calls, memory
        self.recall = MemoryRecall(memory)
        self.search = search
        self.correction = MemoryCorrection(memory)
        self.deletion = MemoryDeletion(memory)
        self.automatic = AutomaticMemory(memory)
        self.memory_tools = MemoryTools(memory, self.automatic, search, search.attachments)

    def embedding_revision(self, content: str) -> str | None:
        """返回当前嵌入配置版本；Agent 每轮都可能选择记忆工具，因此每轮都绑定版本。"""
        embedding = self.memory.embedding.store.read()
        return embedding.revision if embedding else None

    def same_embedding_revision(self, expected: str | None) -> bool:
        """续跑只核验原来涉及的嵌入配置，不把记忆数量变化误当配置修改。"""
        current=self.memory.embedding.store.read()
        return expected is None or (current is not None and current.revision==expected)

    def scope(self, content: str, settings: StoredSettings) -> list[str | None]:
        """返回聊天、嵌入及搜索配置版本组成的幂等范围，防止操作结果跨配置复用。"""
        search = self.search.settings.read()
        return [content, settings.revision, self.embedding_revision(content), search.revision if search else None]

    def reserve_first(self, content: str, settings: StoredSettings, operation: OperationContext, user_id: UUID | None = None) -> UUID | None:
        """不做前置预留；Agent 工具与最终回答分别在真实调用点创建调用记录，始终返回空。"""
        return None

    def prepare_answer(self, user_id: UUID, content: str, operation: OperationContext, reserved: UUID | None) -> list[ModelMessage]:
        """提供正式相关记忆及原生自动处理结果，回答只能根据实际状态声称已记住。"""
        with self.memory.database.connect() as connection:
            row=connection.execute('SELECT unit,status FROM model_calls WHERE id=%s',(reserved,)).fetchone() if reserved else None
        material=self.recall.prepare(user_id,content,operation,reserved,skip=bool(row and row['unit']=='chat_tokens'))
        return material

    def prepare_response(
        self,
        conversation_id: UUID,
        account_id: UUID,
        user_id: UUID,
        message_id: UUID,
        settings: StoredSettings,
        operation: OperationContext,
        reserved: UUID | None,
        rows: list[dict[str, Any]],
    ) -> AnswerPreparation:
        """统一组合有效文档清单、全部图片、长期记忆和历史，文档正文由模型按需调用工具读取。"""
        content = str(rows[-1]['content'])
        chat_reserved = self.chat_reservation(content, reserved)
        material = self.search.attachments.prepare(conversation_id, account_id)
        memory_material = self.prepare_answer(user_id, content, operation, reserved)
        context_fragments = []
        if memory_material:
            context_fragments.append(memory_context_fragment())
        if material.messages:
            context_fragments.append(attachment_context_fragment())
        system_prompt: ModelMessage = {
            'role': 'system',
            'content': compose_system_prompt(*context_fragments),
        }
        data_messages: list[ModelMessage] = [*memory_material, *material.messages]
        conversation: list[ModelMessage] = [system_prompt, *data_messages]
        conversation.extend({'role': str(row['role']), 'content': str(row['content'])} for row in rows)
        return AnswerPreparation(conversation, material, chat_reserved)

    def chat_reservation(self, content: str, reserved: UUID | None) -> UUID | None:
        """仅复用尚未启动的聊天预留；保存或召回已经用过的请求不得再次启动。"""
        if reserved is None:
            return None
        with self.memory.database.connect() as connection:
            row=connection.execute('SELECT unit,status FROM model_calls WHERE id=%s',(reserved,)).fetchone()
        return reserved if row and row['unit']=='chat_tokens' and row['status']=='reserved' else None

    def prepare(
        self,
        conversation_id: UUID,
        account_id: UUID,
        user_id: UUID,
        content: str,
        operation: OperationContext,
        reserved: UUID | None,
        settings: StoredSettings | None = None,
        *,
        messages: list[ModelMessage],
        assistant_message_id: UUID,
        on_content: Callable[[str], None] | None = None,
        material: AttachmentContext | None = None,
    ) -> ToolOutcome:
        """把完整回答上下文交给统一流式工具循环。"""
        if settings is None:
            return ToolOutcome(None, False)
        source_attachment_ids = self.material_attachment_ids(material)
        return self.memory_tools.prepare(
            conversation_id,
            account_id,
            user_id,
            content,
            settings,
            operation,
            messages=messages,
            assistant_message_id=assistant_message_id,
            on_content=on_content,
            source_attachment_ids=source_attachment_ids,
        )

    @staticmethod
    def material_attachment_ids(material: AttachmentContext | None) -> list[UUID]:
        """从文档清单中提取稳定附件标识，供搜索操作记录关联来源。"""
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
