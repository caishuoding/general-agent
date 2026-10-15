"""只把当前用户已明确批准的事实交给 Mem0，并核对部分提交及来源。"""

import os
from collections.abc import Iterator, Callable
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from general_agent.budget import BudgetPaused, OperationContext
from general_agent.calls import CallGateway
from general_agent.database import Database
from general_agent.embedding import EmbeddingConfiguration
from general_agent.provider import ModelFailure
from general_agent.settings import StoredEmbeddingSettings

if TYPE_CHECKING:
    from general_agent.mem0_adapter import PgMemory


class MemoryService:
    """隐藏 SDK 和独立事务恢复，消息标识使回答重试复用同一保存动作。"""

    def __init__(self, database: Database, embedding: EmbeddingConfiguration, calls: CallGateway, directory: Path) -> None:
        """绑定持久化与统一调用网关；缺少嵌入时普通聊天仍可启动。"""
        self.database, self.embedding, self.calls, self.directory = database, embedding, calls, directory

    @contextmanager
    def session(self, settings: StoredEmbeddingSettings, operation: OperationContext | None = None, reserved: UUID | None = None, before_write: Callable[[], None] | None = None) -> Iterator['PgMemory']:
        """按操作创建 SDK 并携带可选写入前来源核验；首次导入前关闭遥测和限定辅助目录。"""
        os.environ['MEM0_TELEMETRY'] = 'False'
        os.environ['MEM0_DIR'] = str(self.directory / 'mem0-runtime')
        from general_agent.mem0_adapter import PgMemory
        with self.database.connect() as connection:
            connection.autocommit = True
            connection.execute('SELECT pg_advisory_lock(91402705)')
            memory = PgMemory(self.database, settings, self.calls.bind(operation) if operation else None, reserved,before_write)
        try:
            yield memory
        finally:
            memory.close()

    def action(self, message_id: UUID) -> dict[str, Any] | None:
        """只返回原用户明确保存动作，候选确认的多个独立身份不会混入回答重试。"""
        with self.database.connect() as connection:
            return connection.execute('SELECT * FROM memory_actions WHERE id=%s', (uuid5(message_id,'explicit-memory:0'),)).fetchone()

    def list(self) -> list[dict[str, Any]]:
        """展示消息、附件及更正来源；只有已完整提交的事实才从正式存储核验。"""
        with self.database.connect() as connection:
            rows = connection.execute('SELECT a.*, c.title AS conversation_title FROM memory_actions a JOIN conversations c ON c.id=a.conversation_id ORDER BY a.created_at DESC,a.id').fetchall()
            for row in rows:
                row['attachment_sources']=connection.execute('SELECT s.* FROM attachment_memory_candidates s JOIN memory_candidates c ON c.id=s.candidate_id WHERE c.memory_action_id=%s',(row['id'],)).fetchall()
                row["updates"] = connection.execute("SELECT u.*,c.title AS conversation_title FROM memory_updates u LEFT JOIN conversations c ON c.id=u.source_conversation_id WHERE action_id=%s ORDER BY u.created_at,u.id",(row["id"],)).fetchall()
        if not rows or not any(row["status"] == "complete" for row in rows):
            return rows
        settings = self.embedding.store.read()
        if settings is None:
            return [{**row, 'error': '嵌入配置不可读，暂时无法核验正式记忆。'} for row in rows]
        with self.session(settings) as memory:
            for row in rows:
                if row['status'] == 'complete':
                    entry = memory.get(str(row['memory_id']))
                    if entry is None or entry.get('user_id') != 'local-person':
                        row.update(status='failed', error='正式记忆暂时不可读取，请重试核对。')
                    else:
                        row['fact'] = entry['memory']
        return rows

    def save(self, conversation_id: UUID, message_id: UUID, fact: str, operation: OperationContext, reserved: UUID | None = None, action_id: UUID | None = None, validate_source: Callable[[], None] | None = None) -> dict[str, Any]:
        """以稳定动作登记批准并复用部分向量；附件在嵌入回传后和发布前重新核验来源。"""
        if validate_source:
            validate_source()
        action_id = action_id or uuid5(message_id, 'explicit-memory:0')
        with self.database.connect() as lock:
            lock.autocommit = True
            lock.execute('SELECT pg_advisory_lock(%s)', (action_id.int % (2**63 - 1),))
            lock.execute("INSERT INTO memory_actions(id,message_id,conversation_id,operation_id,fact,status) VALUES(%s,%s,%s,%s,%s,'pending') ON CONFLICT(id) DO NOTHING", (action_id,message_id,conversation_id,operation.operation_id,fact))
            existing = lock.execute('SELECT * FROM memory_actions WHERE id=%s',(action_id,)).fetchone()
            assert existing is not None
            if existing['fact'] != fact:
                raise ValueError('保存内容与原批准不一致，请重新明确具体事实。')
            if existing['status'] == 'complete':
                return existing
            lock.execute("UPDATE memory_actions SET status='saving',error=NULL,operation_id=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (operation.operation_id,action_id))
            try:
                settings = self.embedding.verified()
                self.embedding.bind_index('personal_memories', settings)
                with self.session(settings, operation, reserved,validate_source) as memory:
                    rows = memory.vector_store.list(filters={'action_id': str(action_id), 'user_id': 'local-person'}, limit=2)[0]
                    if len(rows) > 1:
                        raise ValueError('发现重复的正式记忆，需先核对存储，未继续保存。')
                    if rows:
                        memory_id = str(rows[0].id)
                    else:
                        result = memory.add(fact, user_id='local-person', metadata={'action_id': str(action_id), 'source_message_id': str(message_id), 'source_conversation_id': str(conversation_id)}, infer=False)
                        memory_id = result['results'][0]['id']
                    entry = memory.get(memory_id)
                    if entry is None or entry['memory'] != fact:
                        raise ValueError('正式记忆与已批准事实不一致，保存结果待核对。')
                    if not memory.history(memory_id):
                        memory.db.add_history(memory_id, None, fact, 'ADD', created_at=entry['created_at'], role='user')
                if validate_source:
                    validate_source()
                lock.execute("UPDATE memory_actions SET status='complete',memory_id=%s,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (memory_id,action_id))
            except BudgetPaused:
                lock.execute("UPDATE memory_actions SET status='pending',error='等待预算许可，尚未确认保存。' WHERE id=%s", (action_id,))
                raise
            except Exception as error:
                detail = str(error) if isinstance(error, (ModelFailure, ValueError)) else '记忆保存未完成，请重试；重试会先核对已有写入。'
                lock.execute("UPDATE memory_actions SET status='failed',error=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (detail,action_id))
                raise ValueError(detail) from None
            result = lock.execute('SELECT * FROM memory_actions WHERE id=%s',(action_id,)).fetchone()
            assert result is not None
            return result
