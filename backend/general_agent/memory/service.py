"""只把当前用户已明确批准的事实交给 Mem0，并核对部分提交及来源。"""

import os
from collections.abc import Iterator, Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List
from uuid import UUID, uuid5

import httpx

from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.provider import ModelFailure
from general_agent.memory.mem0_adapter import Mem0Client

class MemoryService:
    """封装 Mem0 REST 会话和业务事务恢复，消息标识使回答重试复用同一保存动作。"""

    def __init__(self, database: Database, embedding: Any, calls: CallGateway, directory: Path, transport: httpx.BaseTransport | None = None) -> None:
        """绑定业务持久化、统一调用网关和附件配置目录。"""
        self.database, self.embedding, self.calls, self.directory = database, embedding, calls, directory
        self.transport = transport
        # 单机默认使用本地用户；测试通过独立用户标识隔离共享 Mem0 服务中的数据。
        self.user_id = os.getenv("MEM0_USER_ID", "local-person")

    @contextmanager
    def session(self) -> Iterator[Mem0Client]:
        """打开独立 Mem0 REST 会话，并在退出时释放 HTTP 连接池。"""
        client = Mem0Client(transport=self.transport)
        try:
            yield client
        finally:
            client.close()

    @contextmanager
    def maintenance(self) -> Iterator[None]:
        """先取得允许维护期间查看及删除的记忆租约，调用方随后才可锁业务来源行。"""
        yield

    def retainable(self, actions: list[dict[str, Any]]) -> set[UUID]:
        """核验当前活动空间中的完整事实，隐藏配置与 SDK；来源删除调用方须先持维护租约。"""
        valid: set[UUID]=set()
        if any(row['status']=='complete' and row['memory_id'] for row in actions):
            with self.session() as sdk:
                for row in actions:
                    if row['status']=='complete' and row['memory_id']:
                        entry=sdk.get(str(row['memory_id']))
                        if entry and entry.get('user_id')==self.user_id and entry.get('memory')==row['fact']:
                            valid.add(row['id'])
        return valid

    def action(self, message_id: UUID) -> dict[str, Any] | None:
        """只返回原用户明确保存动作，自动记忆产生的其他动作不会混入回答重试。"""
        with self.database.connect() as connection:
            return connection.execute('SELECT * FROM memory_actions WHERE id=%s', (uuid5(message_id,'explicit-memory:0'),)).fetchone()

    def list(self) -> list[dict[str, Any]]:
        """展示未删除条目、清理状态及消息和更正来源，正式有效事实再从 SDK 核验。"""
        with self.database.connect() as connection:
            rows = connection.execute("SELECT a.*, c.title AS conversation_title FROM memory_actions a LEFT JOIN conversations c ON c.id=a.conversation_id WHERE a.status <> 'deleted' ORDER BY a.created_at DESC,a.id").fetchall()
            for row in rows:
                row["deletion"] = connection.execute('SELECT status,error FROM memory_deletions WHERE action_id=%s',(row['id'],)).fetchone()
                row["updates"] = connection.execute("SELECT u.*,c.title AS conversation_title FROM memory_updates u LEFT JOIN conversations c ON c.id=u.source_conversation_id WHERE action_id=%s ORDER BY u.created_at,u.id",(row["id"],)).fetchall()
        if not rows or not any(row["status"] == "complete" for row in rows):
            return rows
        with self.session() as memory:
            for row in rows:
                if row['status'] == 'complete':
                    entry = memory.get(str(row['memory_id']))
                    if entry is None or entry.get('user_id') != self.user_id:
                        row.update(status='failed', error='正式记忆暂时不可读取，请重试核对。')
                    else:
                        row['fact'] = entry['memory']
        return rows

    def source_suppressed(self, message_id: UUID) -> bool:
        """抑制已忘记的旧消息再次触发自动记忆。"""
        with self.database.connect() as connection:
            return connection.execute('SELECT message_id FROM memory_source_suppressions WHERE message_id=%s',(message_id,)).fetchone() is not None

    def guard_source(self, connection: Any, conversation_id: UUID | None, message_id: UUID | None, operation: OperationContext) -> None:
        """在当前事务先锁来源会话再核对操作；与来源删除冻结串行，管理页动作没有原消息要求。"""
        if conversation_id is not None:
            source=connection.execute('SELECT id FROM conversations WHERE id=%s AND NOT deleting FOR SHARE',(conversation_id,)).fetchone()
            if source is None or not connection.execute("SELECT id FROM messages WHERE id=%s AND conversation_id=%s AND role='user'",(message_id,conversation_id)).fetchone():
                raise ValueError('保存来源已删除或停用，旧批准不能继续提交。')
        active=connection.execute('SELECT cancelled,invalidated FROM cloud_operations WHERE id=%s FOR SHARE',(operation.operation_id,)).fetchone()
        if active is None or active['cancelled'] or active['invalidated']:
            raise ValueError('操作已停止或来源已删除，旧批准不能继续提交。')

    def save(self, conversation_id: UUID, message_id: UUID, fact: str, operation: OperationContext, reserved: UUID | None = None, action_id: UUID | None = None, validate_source: Callable[[], None] | None = None, cached: dict[str, List[float]] | None = None) -> dict[str, Any]:
        """按稳定身份核对部分提交，可复用原生向量；空间租约至发布结束，删除墓碑优先。"""
        if validate_source:
            validate_source()
        action_id = action_id or uuid5(message_id, 'explicit-memory:0')
        with self.database.connect() as lock:
            lock.autocommit = True
            lock.execute('SELECT pg_advisory_lock(%s)', (action_id.int % (2**63 - 1),))
            with lock.transaction():
                self.guard_source(lock,conversation_id,message_id,operation)
                lock.execute("INSERT INTO memory_actions(id,message_id,conversation_id,operation_id,fact,status) VALUES(%s,%s,%s,%s,%s,'pending') ON CONFLICT(id) DO NOTHING", (action_id,message_id,conversation_id,operation.operation_id,fact))
            existing = lock.execute('SELECT * FROM memory_actions WHERE id=%s',(action_id,)).fetchone()
            assert existing is not None
            if existing['status'] in ('deleting','deleted'):
                raise ValueError('该长期记忆已删除，旧保存批准已失效。')
            if existing['fact'] != fact:
                raise ValueError('保存内容与原批准不一致，请重新明确具体事实。')
            if existing['status'] == 'complete':
                return existing
            lock.execute("UPDATE memory_actions SET status='saving',error=NULL,operation_id=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status NOT IN ('deleting','deleted')", (operation.operation_id,action_id))
            try:
                with self.session() as memory:
                    rows = [item for item in memory.get_all(self.user_id) if (item.get('metadata') or {}).get('action_id') == str(action_id)]
                    if len(rows) > 1:
                        raise ValueError('发现重复的正式记忆，需先核对存储，未继续保存。')
                    if rows:
                        memory_id = str(rows[0]['id'])
                    else:
                        result = memory.add([{'role':'user','content':fact}], user_id=self.user_id, metadata={'action_id': str(action_id), 'source_message_id': str(message_id), 'source_conversation_id': str(conversation_id)}, infer=False)
                        found = memory._results(result)
                        if not found or not found[0].get('id'):
                            raise ValueError('Mem0 未返回新记忆 ID。')
                        memory_id = str(found[0]['id'])
                    entry = memory.get(memory_id)
                    if entry is None or entry['memory'] != fact:
                        raise ValueError('正式记忆与已批准事实不一致，保存结果待核对。')
                if validate_source:
                    validate_source()
                with lock.transaction():
                    self.guard_source(lock,conversation_id,message_id,operation)
                    changed=lock.execute("UPDATE memory_actions SET status='complete',memory_id=%s,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status NOT IN ('deleting','deleted') RETURNING id", (memory_id,action_id)).fetchone()
                if changed is None:
                    raise ValueError('该长期记忆已删除，迟到的保存未生效。')
            except Exception as error:
                detail = str(error) if isinstance(error, (ModelFailure, ValueError)) else '记忆保存未完成，请重试；重试会先核对已有写入。'
                lock.execute("UPDATE memory_actions SET status='failed',error=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status NOT IN ('deleting','deleted')", (detail,action_id))
                raise ValueError(detail) from None
            result = lock.execute('SELECT * FROM memory_actions WHERE id=%s',(action_id,)).fetchone()
            assert result is not None
            return result

    def register_existing(self, conversation_id: UUID, message_id: UUID, fact: str, memory_id: str, operation: OperationContext, action_id: UUID) -> dict[str, Any]:
        """登记 Mem0 2.x 已写入的 ADD 结果，不重复写向量或历史。"""
        with self.database.connect() as connection:
            self.guard_source(connection, conversation_id, message_id, operation)
            connection.execute(
                "INSERT INTO memory_actions(id,message_id,conversation_id,operation_id,memory_id,fact,status) VALUES(%s,%s,%s,%s,%s,%s,'complete') ON CONFLICT(id) DO UPDATE SET memory_id=EXCLUDED.memory_id,fact=EXCLUDED.fact,status='complete',error=NULL,updated_at=CURRENT_TIMESTAMP",
                (action_id, message_id, conversation_id, operation.operation_id, memory_id, fact),
            )
            row = connection.execute("SELECT * FROM memory_actions WHERE id=%s", (action_id,)).fetchone()
            assert row is not None
            return row

    def begin_explicit(self, conversation_id: UUID, message_id: UUID, operation: OperationContext, action_id: UUID) -> dict[str, Any]:
        """在外部推断前登记明确保存动作，使中途停止或进程中断仍能核对真实结果。"""
        with self.database.connect() as connection:
            self.guard_source(connection, conversation_id, message_id, operation)
            connection.execute(
                "INSERT INTO memory_actions(id,message_id,conversation_id,operation_id,fact,status) VALUES(%s,%s,%s,%s,'','pending') ON CONFLICT(id) DO NOTHING",
                (action_id, message_id, conversation_id, operation.operation_id),
            )
            row = connection.execute('SELECT * FROM memory_actions WHERE id=%s', (action_id,)).fetchone()
            assert row is not None
            return row

    def complete_explicit(self, conversation_id: UUID, message_id: UUID, operation: OperationContext, action_id: UUID, fact: str, memory_id: str) -> dict[str, Any]:
        """核对来源仍有效后完成明确保存动作，迟到的结果不能覆盖已停止或已删除来源。"""
        with self.database.connect() as connection:
            self.guard_source(connection, conversation_id, message_id, operation)
            changed = connection.execute(
                "UPDATE memory_actions SET fact=%s,memory_id=%s,status='complete',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status NOT IN ('deleting','deleted') RETURNING *",
                (fact, memory_id, action_id),
            ).fetchone()
            if changed is None:
                raise ValueError('该长期记忆已删除，迟到的保存未生效。')
            return changed

    def fail_explicit(self, action_id: UUID, detail: str) -> None:
        """记录明确保存失败；来源已停止时仍保留待核对失败状态。"""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE memory_actions SET fact='',memory_id=NULL,status='failed',error=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status NOT IN ('complete','deleting','deleted')",
                (detail, action_id),
            )

    def apply_native_update(self, conversation_id: UUID, message_id: UUID, operation: OperationContext, memory_id: str, fact: str, previous_fact: str | None = None) -> dict[str, Any]:
        """把 Mem0 已完成的语义更新同步到业务账本，并清理同一来源的待定明确动作。"""
        with self.database.connect() as connection:
            self.guard_source(connection, conversation_id, message_id, operation)
            row = connection.execute(
                "UPDATE memory_actions SET fact=%s,status='complete',version=version+1,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE memory_id=%s AND status='complete' RETURNING *",
                (fact, memory_id),
            ).fetchone()
            if row is None:
                raise ValueError('Mem0 更新了未登记的记忆，未同步业务账本。')
            update_id = uuid5(message_id, 'native-update:' + memory_id + ':' + fact)
            connection.execute(
                "INSERT INTO memory_updates(id,action_id,operation_id,target_version,previous_fact,fact,status,source_kind,source_message_id,source_conversation_id) VALUES(%s,%s,%s,%s,%s,%s,'complete','chat',%s,%s) ON CONFLICT(id) DO NOTHING",
                (update_id, row['id'], operation.operation_id, row['version'], previous_fact or '', fact, message_id, conversation_id),
            )
            connection.execute("DELETE FROM memory_actions WHERE id=%s AND status='pending'", (uuid5(message_id, 'explicit-memory:0'),))
            return row
