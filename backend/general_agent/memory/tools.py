"""为对话智能体注册真实记忆工具，执行前核对观察结果、版本和当前来源。"""

from dataclasses import dataclass, field
import json
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID, uuid5

import httpx
from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

from general_agent.infrastructure.operations import OperationContext
from general_agent.memory.service import MemoryService
from general_agent.memory.correction import MemoryCorrection
from general_agent.memory.deletion import MemoryDeletion
from general_agent.memory.automatic import AutomaticMemory, MemoryCheckpoint
from general_agent.infrastructure.settings import StoredSettings
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.prompts import TOOL_DESCRIPTIONS
from general_agent.search.service import FetchWebInput, SearchService, SearchWebInput, search_web_tool
from general_agent.agent.graph import AgentToolGraph
from general_agent.agent.time import current_time_tool, get_current_time
from general_agent.attachments.service import AttachmentService
from general_agent.attachments.tools import document_tools
from general_agent.infrastructure.tokens import TokenCounter
from general_agent.knowledge.tools import KnowledgeTools, knowledge_tool


class SearchMemoryInput(BaseModel):
    """限制语义查询长度，空查询允许查看现有条目。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(max_length=1000)


class AddMemoryInput(BaseModel):
    """添加工具不接受自由事实文本，服务端只使用已持久化的真实对话。"""
    model_config = ConfigDict(extra='forbid', strict=True)


class DeleteMemoryInput(BaseModel):
    """删除必须引用本次查找返回的具体记忆与版本。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    memory_id: str
    version: int = Field(ge=1)


class UpdateMemoryInput(DeleteMemoryInput):
    """更新必须提供用户当前表达的完整新事实。"""
    fact: str = Field(min_length=1, max_length=5000)


@dataclass(frozen=True)
class ToolOutcome:
    """返回最终文字、模型消息快照及已交付的知识库来源候选。"""

    answer: str | None
    used_tools: bool
    messages: list[dict[str, Any]] | None = None
    knowledge_source_candidates: list[dict[str, Any]] = field(default_factory=list)


def memory_tools() -> list[dict[str, Any]]:
    """返回 OpenAI 兼容函数定义，模型自行选择查找、更新或删除。"""
    definitions: list[tuple[str, str, type[BaseModel]]] = [
        ('add_memory', TOOL_DESCRIPTIONS['add_memory'], AddMemoryInput),
        ('search_memories', TOOL_DESCRIPTIONS['search_memories'], SearchMemoryInput),
        ('update_memory', TOOL_DESCRIPTIONS['update_memory'], UpdateMemoryInput),
        ('delete_memory', TOOL_DESCRIPTIONS['delete_memory'], DeleteMemoryInput),
    ]
    return [{'type': 'function', 'function': {'name': name, 'description': description, 'parameters': model.model_json_schema()}} for name, description, model in definitions]


def shared_tools(knowledge: bool = False, documents: bool = False) -> list[dict[str, Any]]:
    """按当前会话的知识库选择和有效文档清单返回公共工具定义，不包含记忆及编排权限。"""
    return [search_web_tool(), search_web_tool("fetch_web"),
            *(document_tools() if documents else []), current_time_tool(),
            *([knowledge_tool()] if knowledge else [])]


def agent_tools(knowledge: bool = False, documents: bool = False) -> list[dict[str, Any]]:
    """按会话资料可用性返回业务工具定义；包含记忆工具，编排工具由外层另行附加。"""
    return [*memory_tools(), *shared_tools(knowledge, documents)]


def worker_tools(knowledge: bool = False, documents: bool = False) -> list[dict[str, Any]]:
    """按会话资料可用性返回子智能体公共工具定义，排除记忆写入与编排能力。"""
    return shared_tools(knowledge, documents)


class MemoryTools:
    """隐藏记忆与搜索工具协议、版本检查及可恢复执行。"""

    def __init__(self, memory: MemoryService, automatic: AutomaticMemory, search: SearchService | None = None,
                 attachments: AttachmentService | None = None, knowledge: KnowledgeTools | None = None) -> None:
        """绑定各自独立的记忆、搜索、附件与知识库边界，兼容既有独立工具测试。"""
        self.memory, self.automatic, self.web_search = memory, automatic, search
        self.graph = AgentToolGraph()
        self.attachments = attachments
        self.knowledge = knowledge

    def add(self, checkpoint: MemoryCheckpoint, arguments: dict[str, Any], settings: StoredSettings) -> dict[str, Any]:
        """从已持久化的少量会话构造 Mem0 输入；工具参数不能伪造记忆正文，返回实际处理状态。"""
        AddMemoryInput.model_validate(arguments)
        checkpoint.guard()
        with self.memory.database.connect() as connection:
            current = connection.execute('SELECT position FROM messages WHERE id=%s AND conversation_id=%s', (checkpoint.message_id, checkpoint.conversation_id)).fetchone()
            if current is None:
                raise ValueError('当前用户消息已经不可用。')
            rows = connection.execute(
                "SELECT role,content FROM messages WHERE conversation_id=%s AND status='complete' AND position<=%s ORDER BY position DESC LIMIT 6",
                (checkpoint.conversation_id, current['position']),
            ).fetchall()
        messages = [{'role': str(row['role']), 'content': str(row['content'])} for row in reversed(rows)]
        detail = self.automatic.process_messages(checkpoint.conversation_id, checkpoint.message_id, messages, settings, checkpoint.operation, explicit=True)
        if detail.startswith('自动记忆未完成：'):
            raise ValueError(detail)
        return {'status': 'complete', 'action': 'memory_added', 'detail': detail}

    def search(self, checkpoint: MemoryCheckpoint, query: str) -> dict[str, Any]:
        """通过 Mem0 混合召回或本地列表取得有效记忆，再核对业务版本并登记观察。"""
        checkpoint.guard()
        account_id = self.memory.account_id_for_conversation(checkpoint.conversation_id)
        rows = [row for row in self.memory.list(account_id) if row['status'] == 'complete']
        if query.strip() and rows:
            with self.memory.session() as sdk:
                hits = sdk._results(sdk.search(query, user_id=str(account_id), limit=50, filters={'user_id': str(account_id)}))
            rank = {str(hit['id']): position for position, hit in enumerate(hits)}
            rows = [row for row in rows if str(row['memory_id']) in rank]
            # 按 Mem0 的实际语义相关性排序，不让数据库创建时间覆盖结果。
            def ranking(row: dict[str, Any]) -> int:
                """返回正式 Mem0 标识在检索结果中的顺序。"""
                return rank[str(row['memory_id'])]
            rows.sort(key=ranking)
        rows = rows[:50]
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, checkpoint.conversation_id, checkpoint.message_id, checkpoint.operation)
            for row in rows:
                current = connection.execute("SELECT id FROM memory_actions WHERE id=%s AND version=%s AND status='complete' FOR SHARE", (row['id'], row['version'])).fetchone()
                if current is None:
                    raise ValueError('检索期间记忆已变化，请重新查找。')
                connection.execute('INSERT INTO memory_tool_observations(message_id,action_id,version) VALUES(%s,%s,%s) ON CONFLICT(message_id,action_id) DO UPDATE SET version=EXCLUDED.version', (checkpoint.message_id, row['id'], row['version']))
        return {'memories': [{'memory_id': str(row['id']), 'version': row['version'], 'fact': row['fact']} for row in rows], 'limit': 50}

    def execute(
        self,
        checkpoint: MemoryCheckpoint,
        name: str,
        arguments: dict[str, Any],
        settings: StoredSettings,
        source_attachment_ids: list[UUID] | None = None,
        document_budget: int = 0,
        token_counter: TokenCounter | None = None,
    ) -> dict[str, Any]:
        """执行授权工具；文档阅读使用当前回答 LLM 计数，记忆变更核对观察版本。"""
        checkpoint.guard()
        if name == 'search_knowledge':
            if self.knowledge is None:
                raise ValueError('知识库工具未配置。')
            account_id = self.memory.account_id_for_conversation(checkpoint.conversation_id)
            result = self.knowledge.search(checkpoint.conversation_id, account_id, arguments, self.memory.calls.bind(checkpoint.operation))
            checkpoint.guard()
            return result
        if name == 'get_current_time':
            return get_current_time(arguments)
        if name in ('search_documents', 'read_document'):
            if self.attachments is None:
                raise ValueError('会话文档工具未配置。')
            account_id = self.memory.account_id_for_conversation(checkpoint.conversation_id)
            if name == 'search_documents':
                return self.attachments.documents.search(checkpoint.conversation_id, account_id, arguments)
            return self.attachments.documents.read(checkpoint.conversation_id, account_id, arguments,
                                                   token_counter or TokenCounter.for_settings(settings, self.memory.directory), document_budget)
        if name == 'add_memory':
            return self.add(checkpoint, arguments, settings)
        if name == 'search_memories':
            return self.search(checkpoint, SearchMemoryInput.model_validate(arguments).query)
        if name in ('search_web', 'fetch_web'):
            if self.web_search is None:
                raise ValueError('联网搜索工具未配置。')
            search_input = SearchWebInput.model_validate(arguments) if name == "search_web" else None
            query = search_input.query if search_input is not None else FetchWebInput.model_validate(arguments).url
            return self.web_search.tool(
                checkpoint.conversation_id,
                checkpoint.message_id,
                query,
                checkpoint.operation,
                source_attachment_ids,
                kind="search" if name == "search_web" else "fetch",
                ori_question=search_input.ori_question if search_input is not None else None,
            )
        if name not in ('update_memory', 'delete_memory'):
            raise ValueError('未知的工具。')
        mutation_input: UpdateMemoryInput | DeleteMemoryInput = UpdateMemoryInput.model_validate(arguments) if name == 'update_memory' else DeleteMemoryInput.model_validate(arguments)
        action_id = UUID(mutation_input.memory_id)
        key = checkpoint.key('mutation', [name, arguments])
        cached = checkpoint.get(key)
        if cached is not None:
            return dict(cached)
        with self.memory.database.connect() as connection:
            account_id = self.memory.account_id_for_conversation(checkpoint.conversation_id)
            observed = connection.execute('SELECT version FROM memory_tool_observations WHERE message_id=%s AND action_id=%s', (checkpoint.message_id, action_id)).fetchone()
            entry = connection.execute('SELECT status,version FROM memory_actions WHERE id=%s AND user_id=%s', (action_id, account_id)).fetchone()
        if observed is None or observed['version'] != mutation_input.version or entry is None:
            raise ValueError('请先查找具体记忆并使用返回的版本，不能猜测标识。')
        identifier = uuid5(checkpoint.message_id, key)
        if name == 'update_memory' and isinstance(mutation_input, UpdateMemoryInput):
            result = MemoryCorrection(self.memory).apply(action_id, mutation_input.fact, mutation_input.version, identifier, checkpoint.operation, checkpoint.message_id, checkpoint.conversation_id, account_id=account_id)
            public = {'status': result['status'], 'memory_id': mutation_input.memory_id, 'action': 'updated'}
        else:
            if entry['status'] not in ('deleting', 'deleted') and entry['version'] != mutation_input.version:
                raise ValueError('记忆已变化，请重新查找后再删除。')
            result = MemoryDeletion(self.memory).delete(action_id, checkpoint.operation.operation_id, mutation_input.version, account_id=account_id)
            public = {'status': result['status'], 'memory_id': mutation_input.memory_id, 'action': 'deleted'}
        checkpoint.put(key, public)
        return public

    def prepare(
        self,
        conversation_id: UUID,
        account_id: UUID,
        message_id: UUID,
        content: str,
        settings: StoredSettings,
        operation: OperationContext,
        *,
        messages: list[ModelMessage],
        assistant_message_id: UUID,
        on_content: Callable[[str], None] | None = None,
        source_attachment_ids: list[UUID] | None = None,
    ) -> ToolOutcome:
        """按本轮 LLM 分词核算预算，命中原图分别核验后加入下一轮模型上下文。"""
        if self.memory.account_id_for_conversation(conversation_id) != account_id:
            raise LookupError("会话不存在。")
        counter = TokenCounter.for_settings(settings, self.memory.directory)
        document_budget = 0
        injected_images: set[UUID] = set()

        def update_document_budget(current_messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> None:
            """读取前用实际模型用量和当前消息核对预算，包含图片开销及同批已返回内容。"""
            nonlocal document_budget
            document_budget = max(0, counter.available(settings, current_messages, tools) - 256)
            with self.memory.database.connect() as connection:
                usage = connection.execute(
                    "SELECT prompt_tokens,completion_tokens FROM model_calls WHERE assistant_message_id=%s "
                    "AND unit='chat_tokens' ORDER BY created_at DESC,id DESC LIMIT 1",
                    (assistant_message_id,),
                ).fetchone()
            if usage is not None and usage['prompt_tokens'] is not None:
                last_assistant = max((index for index, message in enumerate(current_messages)
                                      if message['role'] == 'assistant'), default=len(current_messages))
                appended = counter.count(json.dumps(current_messages[last_assistant:], ensure_ascii=False))
                observed = settings.context_window_tokens - settings.output_reserve_tokens - usage['prompt_tokens'] - (usage['completion_tokens'] or 0) - appended - 768
                document_budget = max(0, min(document_budget, observed))
            elif any(isinstance(message.get('content'), list) and any(part.get('type') == 'file' for part in message['content'])
                     for message in current_messages):
                # 文件身份不能代表图片实际 Token；缺少真实用量时不能假定剩余空间。
                document_budget = 0

        def execute(current: MemoryCheckpoint, name: str, arguments: dict[str, Any], current_settings: StoredSettings) -> dict[str, Any]:
            """把模型请求交给业务执行器，预算仅应用于原文读取，不裁剪检索切片。"""
            return self.execute(current, name, arguments, current_settings, source_attachment_ids, document_budget, counter)

        def tool_images(results: list[dict[str, Any]]) -> list[ModelMessage]:
            """按切片来源加载附件或会话绑定知识库原图，共享单批五张和去重限制。"""
            messages: list[ModelMessage] = []
            selected: list[UUID] = []
            knowledge_results = [
                result for result in results
                if any(isinstance(item, dict) and 'knowledge_base_id' in item
                       for item in result.get('results', []) if isinstance(result.get('results'), list))
            ]
            attachment_results = [
                result for result in results if not any(result is knowledge for knowledge in knowledge_results)
            ]
            try:
                if self.attachments is not None:
                    message, selected = self.attachments.document_image_message(
                        conversation_id, account_id, attachment_results, injected_images,
                    )
                    if message is not None:
                        messages.append(message)
                if self.knowledge is not None and len(selected) < 5:
                    message, knowledge_selected = self.knowledge.image_message(
                        conversation_id, account_id, knowledge_results,
                        injected_images | set(selected), limit=5 - len(selected),
                    )
                    selected.extend(knowledge_selected)
                    if message is not None:
                        messages.append(message)
                checkpoint.guard()
            except (ValueError, LookupError, OSError, ModelFailure, httpx.HTTPError):
                messages.append({
                    'role': 'user',
                    'content': '部分文档原图本次未能加载，请仅根据工具返回的文字描述回答，并明确无法核对原图。',
                })
            injected_images.update(selected)
            return messages
        checkpoint = MemoryCheckpoint(self.memory, message_id, conversation_id, operation)
        with self.memory.database.connect() as connection:
            previous = connection.execute('SELECT status,result FROM memory_tool_runs WHERE message_id=%s', (message_id,)).fetchone()
        if previous and previous['status'] == 'complete':
            saved = previous['result']
            answer = saved.get('answer')
            restored: list[dict[str, Any]] = [dict(message) for message in messages]
            if answer:
                restored.append({'role': 'assistant', 'content': answer})
            return ToolOutcome(answer, bool(saved.get('used_tools')), restored)
        if self.memory.source_suppressed(message_id):
            return ToolOutcome(None, False)
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, conversation_id, message_id, operation)
            connection.execute("INSERT INTO memory_tool_runs(message_id,operation_id,status) VALUES(%s,%s,'pending') ON CONFLICT(message_id) DO UPDATE SET operation_id=EXCLUDED.operation_id", (message_id, operation.operation_id))
        # 独立工具图也只声明当前会话真实可用的检索能力；图片附件不属于可读取文档。
        knowledge_enabled = self.knowledge is not None and bool(self.knowledge.service.selected(conversation_id, account_id))
        documents_enabled = self.attachments is not None and bool(source_attachment_ids)
        result = self.graph.run(
            messages=cast(list[dict[str, Any]], messages),
            settings=settings,
            calls=self.memory.calls.bind(operation),
            checkpoint=checkpoint,
            tools=agent_tools(knowledge_enabled, documents_enabled),
            assistant_message_id=assistant_message_id,
            on_content=on_content,
            execute_tool=execute,
            on_tool_context=update_document_budget,
            on_tool_results=tool_images,
        )
        outcome = ToolOutcome(result['answer'], bool(result['used_tools']), result['messages'])
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, conversation_id, message_id, operation)
            connection.execute(
                "UPDATE memory_tool_runs SET status='complete',result=%s WHERE message_id=%s",
                (Jsonb({'answer': outcome.answer, 'used_tools': outcome.used_tools}), message_id),
            )
            connection.execute('DELETE FROM memory_native_steps WHERE message_id=%s AND step_key LIKE %s', (message_id, 'tools:%'))
        return outcome
