"""为对话智能体注册真实记忆工具，执行前核对观察结果、版本和当前来源。"""

from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

from general_agent.infrastructure.operations import OperationContext
from general_agent.memory.service import MemoryService
from general_agent.memory.correction import MemoryCorrection
from general_agent.memory.deletion import MemoryDeletion
from general_agent.memory.automatic import AutomaticMemory, MemoryCheckpoint
from general_agent.infrastructure.settings import StoredSettings
from general_agent.conversation.history import HistorySearch
from general_agent.prompts import TOOL_DESCRIPTIONS, tool_planner_instruction
from general_agent.agent.graph import AgentToolGraph


class SearchMemoryInput(BaseModel):
    """限制语义查询长度，空查询允许查看现有条目。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(max_length=1000)


class AddMemoryInput(BaseModel):
    """添加工具不接受自由事实文本，服务端只使用已持久化的真实对话。"""
    model_config = ConfigDict(extra='forbid', strict=True)


class HistorySearchInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=2, max_length=1000)


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
        ('search_chat_history', TOOL_DESCRIPTIONS['search_chat_history'], HistorySearchInput),
        ('search_memories', TOOL_DESCRIPTIONS['search_memories'], SearchMemoryInput),
        ('update_memory', TOOL_DESCRIPTIONS['update_memory'], UpdateMemoryInput),
        ('delete_memory', TOOL_DESCRIPTIONS['delete_memory'], DeleteMemoryInput),
    ]
    return [{'type': 'function', 'function': {'name': name, 'description': description, 'parameters': model.model_json_schema()}} for name, description, model in definitions]


class MemoryTools:
    """隐藏工具协议、版本检查和恢复，管理页与对话工具复用同一记忆操作。"""

    def __init__(self, memory: MemoryService, automatic: AutomaticMemory, history: HistorySearch) -> None:
        """只绑定现有记忆模块，工具无权访问其他数据来源。"""
        self.memory, self.automatic, self.history = memory, automatic, history
        self.graph = AgentToolGraph()

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
        """通过 Mem0 语义查找或本地列表取得有效记忆，登记可修改的观察版本。"""
        checkpoint.guard()
        rows = [row for row in self.memory.list() if row['status'] == 'complete']
        if query.strip() and rows:
            with self.memory.session() as sdk:
                hits = sdk._results(sdk.search(query, user_id=self.memory.user_id, limit=50, filters={'user_id': self.memory.user_id}))
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

    def execute(self, checkpoint: MemoryCheckpoint, name: str, arguments: dict[str, Any], settings: StoredSettings) -> dict[str, Any]:
        """执行白名单工具；无观察标识、过时版本和未知参数均不能改变记忆。"""
        checkpoint.guard()
        if name == 'add_memory':
            return self.add(checkpoint, arguments, settings)
        if name == 'search_chat_history':
            history_input = HistorySearchInput.model_validate(arguments)
            return self.history.tool(checkpoint.conversation_id, checkpoint.message_id, history_input.query, checkpoint.operation)
        if name == 'search_memories':
            return self.search(checkpoint, SearchMemoryInput.model_validate(arguments).query)
        if name not in ('update_memory', 'delete_memory'):
            raise ValueError('未知的记忆工具。')
        mutation_input: UpdateMemoryInput | DeleteMemoryInput = UpdateMemoryInput.model_validate(arguments) if name == 'update_memory' else DeleteMemoryInput.model_validate(arguments)
        action_id = UUID(mutation_input.memory_id)
        key = checkpoint.key('mutation', [name, arguments])
        cached = checkpoint.get(key)
        if cached is not None:
            return dict(cached)
        with self.memory.database.connect() as connection:
            observed = connection.execute('SELECT version FROM memory_tool_observations WHERE message_id=%s AND action_id=%s', (checkpoint.message_id, action_id)).fetchone()
            entry = connection.execute('SELECT status,version FROM memory_actions WHERE id=%s', (action_id,)).fetchone()
        if observed is None or observed['version'] != mutation_input.version or entry is None:
            raise ValueError('请先查找具体记忆并使用返回的版本，不能猜测标识。')
        identifier = uuid5(checkpoint.message_id, key)
        if name == 'update_memory' and isinstance(mutation_input, UpdateMemoryInput):
            result = MemoryCorrection(self.memory).apply(action_id, mutation_input.fact, mutation_input.version, identifier, checkpoint.operation, checkpoint.message_id, checkpoint.conversation_id)
            public = {'status': result['status'], 'memory_id': mutation_input.memory_id, 'action': 'updated'}
        else:
            if entry['status'] not in ('deleting', 'deleted') and entry['version'] != mutation_input.version:
                raise ValueError('记忆已变化，请重新查找后再删除。')
            result = MemoryDeletion(self.memory).delete(action_id, checkpoint.operation.operation_id, mutation_input.version)
            public = {'status': result['status'], 'memory_id': mutation_input.memory_id, 'action': 'deleted'}
        checkpoint.put(key, public)
        return public

    def prepare(self, conversation_id: UUID, message_id: UUID, content: str, settings: StoredSettings, operation: OperationContext) -> str | None:
        """运行可恢复的 Agent 工具回合；无工具时返回空，使用工具后返回基于结果的最终回答。"""
        checkpoint = MemoryCheckpoint(self.memory, message_id, conversation_id, operation)
        with self.memory.database.connect() as connection:
            previous = connection.execute('SELECT status,result FROM memory_tool_runs WHERE message_id=%s', (message_id,)).fetchone()
        if previous and previous['status'] == 'complete':
            return previous['result'].get('answer') if previous['result'].get('handled') else None
        if self.memory.source_suppressed(message_id):
            return None
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, conversation_id, message_id, operation)
            connection.execute("INSERT INTO memory_tool_runs(message_id,operation_id,status) VALUES(%s,%s,'pending') ON CONFLICT(message_id) DO UPDATE SET operation_id=EXCLUDED.operation_id", (message_id, operation.operation_id))
        with self.memory.database.connect() as connection:
            source = connection.execute('SELECT position FROM messages WHERE id=%s AND conversation_id=%s', (message_id, conversation_id)).fetchone()
            if source is None:
                raise ValueError('当前用户消息已经不可用。')
            history = connection.execute(
                "SELECT role,content FROM messages WHERE conversation_id=%s AND status='complete' AND position<=%s ORDER BY position",
                (conversation_id, source['position']),
            ).fetchall()
        messages: list[dict[str, Any]] = [{'role': 'system', 'content': tool_planner_instruction()}]
        messages.extend({'role': str(row['role']), 'content': str(row['content'])} for row in history)
        result = self.graph.run(
            messages=messages,
            settings=settings,
            calls=self.memory.calls.bind(operation),
            checkpoint=checkpoint,
            tools=memory_tools(),
            execute_tool=self.execute,
        )
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, conversation_id, message_id, operation)
            connection.execute("UPDATE memory_tool_runs SET status='complete',result=%s WHERE message_id=%s", (Jsonb({'handled': result['handled'], 'answer': result['answer']}), message_id))
            connection.execute('DELETE FROM memory_native_steps WHERE message_id=%s AND step_key LIKE %s', (message_id, 'tools:%'))
        return result['answer'] if result['handled'] else None
