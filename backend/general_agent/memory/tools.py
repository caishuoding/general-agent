"""为对话智能体注册真实记忆工具，执行前核对观察结果、版本和当前来源。"""

from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from general_agent.memory.service import MemoryService
from general_agent.memory.correction import MemoryCorrection
from general_agent.memory.deletion import MemoryDeletion
from general_agent.memory.automatic import AutomaticMemory
from general_agent.agent.repository import RunCheckpoint
from general_agent.infrastructure.settings import StoredSettings
from general_agent.prompts import TOOL_DESCRIPTIONS
from general_agent.search.service import FetchWebInput, SearchService, SearchWebInput, search_web_tool
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
    """按会话资料可用性返回业务工具定义；包含记忆工具，编排与 Skill 工具由统一注册表附加。"""
    return [*memory_tools(), *shared_tools(knowledge, documents)]


def worker_tools(knowledge: bool = False, documents: bool = False) -> list[dict[str, Any]]:
    """按会话资料可用性返回子智能体公共工具定义，排除记忆写入与编排能力。"""
    return shared_tools(knowledge, documents)


class MemoryTools:
    """隐藏记忆与搜索工具协议、版本检查及可恢复执行。"""

    def __init__(self, memory: MemoryService, automatic: AutomaticMemory, search: SearchService | None = None,
                 attachments: AttachmentService | None = None, knowledge: KnowledgeTools | None = None) -> None:
        """绑定记忆、搜索、附件与知识库依赖；未提供的资料工具不能执行。"""
        self.memory, self.automatic, self.web_search = memory, automatic, search
        self.attachments = attachments
        self.knowledge = knowledge

    def add(self, checkpoint: RunCheckpoint, arguments: dict[str, Any]) -> dict[str, Any]:
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
        detail = self.automatic.process_messages(checkpoint.conversation_id, checkpoint.message_id, messages, checkpoint.operation, explicit=True)
        if detail.startswith('自动记忆未完成：'):
            raise ValueError(detail)
        return {'status': 'complete', 'action': 'memory_added', 'detail': detail}

    def search(self, checkpoint: RunCheckpoint, query: str) -> dict[str, Any]:
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
        checkpoint: RunCheckpoint,
        name: str,
        arguments: dict[str, Any],
        settings: StoredSettings,
        document_budget: int = 0,
        token_counter: TokenCounter | None = None,
    ) -> dict[str, Any]:
        """在当前 Agent Run 授权范围内执行工具；文档阅读核对预算，记忆变更核对观察版本。"""
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
            return self.add(checkpoint, arguments)
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
                kind="search" if name == "search_web" else "fetch",
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
