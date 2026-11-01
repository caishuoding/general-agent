"""管理图检查点连接、版本和会话执行互斥，不持久化运行客户端。"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata, ChannelVersions
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row

from general_agent.infrastructure.agent_cleanup import forget_checkpoint_material
from general_agent.infrastructure.database import Database

STATE_VERSION = 2


class StateSerializer(JsonPlusSerializer):
    """在检查点编码处剔除私有推理，运行内仍可按服务商协议续传。"""

    def dumps_typed(self, value: Any) -> tuple[str, bytes]:
        """递归清理推理字段，保持框架其他通道及 UUID 的官方编码。"""
        return super().dumps_typed(without_reasoning(value))


def without_reasoning(value: Any) -> Any:
    """只移除结构化私有推理字段，不修改用户正文中的同名文字。"""
    if isinstance(value, dict):
        return {key: without_reasoning(item) for key, item in value.items() if key != 'reasoning_content'}
    if isinstance(value, list):
        return [without_reasoning(item) for item in value]
    if isinstance(value, tuple):
        return tuple(without_reasoning(item) for item in value)
    return value


class ExecutionPaused(BaseException):
    """表示应用停机暂停执行，绕过普通工具失败和回答终结处理。"""


class OwnedSaver(PostgresSaver):
    """为稳定会话线程的每次写入校验回答身份，阻止旧请求覆盖新回答。"""

    def __init__(self, connection: Any, owner: 'Checkpoints', conversation_id: UUID, answer_id: UUID, thread_id: str) -> None:
        """绑定会话、执行线程与原回答，资料撤销后同样拒绝内部图迟到写入。"""
        super().__init__(connection, serde=StateSerializer())
        self.owner, self.conversation_id, self.answer_id = owner, conversation_id, answer_id
        self.thread_id = thread_id

    @contextmanager
    def writable(self) -> Iterator[None]:
        """短暂串行检查归属与写入，不在模型网络请求期间持有会话锁。"""
        with self.owner.conversation(self.conversation_id, wait=True), self.owner.database.connect() as connection:
            row = connection.execute('SELECT m.status,t.answer_id,o.cancelled FROM messages m JOIN cloud_operations o ON o.id=m.operation_id JOIN checkpoint_threads t ON t.thread_id=%s WHERE m.id=%s', (self.thread_id, self.answer_id)).fetchone()
            if not row or row['answer_id'] != self.answer_id or row['cancelled'] or row['status'] in ('stopped', 'interrupted'):
                raise ExecutionPaused()
            yield

    def put(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata,
            new_versions: ChannelVersions) -> RunnableConfig:
        """在归属仍有效时提交完整快照，外部调用迟到则停止写入。"""
        with self.writable():
            return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str,
                   task_path: str = '') -> None:
        """对子步骤和失败写入采用相同的回答归属校验。"""
        with self.writable():
            super().put_writes(config, writes, task_id, task_path)


def supersede_interrupted(connection: Any, conversation_id: UUID) -> None:
    """新消息或重试替代旧中断回答时终结其执行身份，避免无主暂停任务。"""
    rows = connection.execute("UPDATE messages SET status='stopped',error='已由新的请求替代。',progress=NULL WHERE conversation_id=%s AND status='interrupted' RETURNING id", (conversation_id,)).fetchall()
    answers = [row['id'] for row in rows]
    if not answers:
        return
    connection.execute('UPDATE agents SET closed=TRUE WHERE answer_attempt_id=ANY(%s)', (answers,))
    connection.execute("UPDATE agent_runs SET status='cancelled',paused_at=NULL,finished_at=CURRENT_TIMESTAMP WHERE agent_id IN (SELECT id FROM agents WHERE answer_attempt_id=ANY(%s)) AND status IN ('queued','running')", (answers,))
    connection.execute('UPDATE cloud_operations SET cancelled=TRUE WHERE id IN (SELECT operation_id FROM messages WHERE id=ANY(%s) UNION SELECT r.operation_id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=ANY(%s))', (answers, answers))
    connection.execute("UPDATE skill_commands SET status='interrupted',finished_at=CURRENT_TIMESTAMP WHERE answer_id=ANY(%s) AND status='running'", (answers,))


class Checkpoints:
    """使用官方 PostgreSQL saver 保存图状态，连接仅在一次执行期间存在。"""

    def __init__(self, database: Database) -> None:
        """绑定数据库，初始化表动作延迟到应用迁移完成之后。"""
        self.database = database

    def setup(self) -> None:
        """串行初始化独立 schema 中的官方检查点表。"""
        with self.database.connect() as connection:
            connection.autocommit = True
            connection.execute('SELECT pg_advisory_lock(91402702)')
            try:
                connection.execute('CREATE SCHEMA IF NOT EXISTS langgraph')
                with self.saver() as saver:
                    saver.setup()
            finally:
                connection.execute('SELECT pg_advisory_unlock(91402702)')

    @contextmanager
    def saver(self, conversation_id: UUID | None = None, answer_id: UUID | None = None, thread_id: str | None = None) -> Iterator[PostgresSaver]:
        """为当前图提供独立自动提交连接，避免检查点被业务事务回滚。"""
        with psycopg.connect(self.database.url, autocommit=True, row_factory=dict_row,
                            options='-c search_path=langgraph,public', prepare_threshold=0, connect_timeout=5) as connection:
            yield OwnedSaver(connection, self, conversation_id, answer_id, thread_id or str(conversation_id)) if conversation_id and answer_id else PostgresSaver(connection, serde=StateSerializer())

    @contextmanager
    def conversation(self, identifier: UUID, wait: bool = False) -> Iterator[None]:
        """短暂串行化认领或检查点提交；等待写入不占用外部网络调用时段。"""
        with self.database.connect() as connection:
            connection.autocommit = True
            key = identifier.int % (2**63 - 1)
            row = connection.execute('SELECT pg_advisory_lock(%s) AS acquired' if wait else 'SELECT pg_try_advisory_lock(%s) AS acquired', (key,)).fetchone()
            if not wait and (not row or not row['acquired']):
                raise ValueError('本会话执行尚未退出，请稍后继续。')
            try:
                yield
            finally:
                connection.execute('SELECT pg_advisory_unlock(%s)', (key,))

    def values(self, thread_id: str) -> dict[str, Any]:
        """读取最新 State 值供恢复验证，未创建线程时返回空。"""
        with self.saver() as saver:
            saved = saver.get_tuple({'configurable': {'thread_id': thread_id}})
            return dict(saved.checkpoint['channel_values']) if saved else {}

    def discard_answer(self, conversation_id: UUID, answer_id: UUID) -> None:
        """回答完成后清除包含已删除记忆派生材料的会话线程和所有执行线程。"""
        with self.database.connect() as connection:
            forget_checkpoint_material(connection, [conversation_id], [answer_id])

    def prune(self) -> None:
        """清理孤立线程并收缩完成执行历史，只保留最新快照引用的通道版本。"""
        with self.database.connect() as connection:
            removed = connection.execute('SELECT DISTINCT p.thread_id FROM langgraph.checkpoints p LEFT JOIN checkpoint_threads t ON t.thread_id=p.thread_id WHERE t.thread_id IS NULL OR NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id=t.conversation_id AND NOT c.deleting)').fetchall()
            finished = connection.execute("SELECT t.thread_id,t.conversation_id FROM checkpoint_threads t LEFT JOIN messages m ON m.id=t.answer_id WHERE m.status IN ('complete','failed','stopped') OR m.id IS NULL").fetchall()
        with self.saver() as saver:
            for row in removed:
                saver.delete_thread(row['thread_id'])
        for row in finished:
            try:
                with self.conversation(row['conversation_id']), self.database.connect() as connection:
                    connection.execute('DELETE FROM langgraph.checkpoint_writes WHERE thread_id=%s AND checkpoint_id <> (SELECT max(checkpoint_id) FROM langgraph.checkpoints WHERE thread_id=%s)', (row['thread_id'], row['thread_id']))
                    connection.execute('DELETE FROM langgraph.checkpoints WHERE thread_id=%s AND checkpoint_id <> (SELECT max(checkpoint_id) FROM langgraph.checkpoints WHERE thread_id=%s)', (row['thread_id'], row['thread_id']))
                    connection.execute("DELETE FROM langgraph.checkpoint_blobs b WHERE thread_id=%s AND NOT EXISTS (SELECT 1 FROM langgraph.checkpoints p, LATERAL jsonb_each_text(p.checkpoint->'channel_versions') v WHERE p.thread_id=b.thread_id AND p.checkpoint_ns=b.checkpoint_ns AND v.key=b.channel AND v.value=b.version)", (row['thread_id'],))
            except ValueError:
                # 会话仍在运行时延后收缩，避免清理恢复所需的并发写入。
                continue
        with self.database.connect() as connection:
            connection.execute('DELETE FROM checkpoint_threads t WHERE NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id=t.conversation_id AND NOT c.deleting)')

    def register(self, thread_id: str, conversation_id: UUID, answer_id: UUID) -> None:
        """只允许仍在生成的回答登记线程，延迟启动的旧线程不能夺取归属。"""
        with self.database.connect() as connection:
            saved = connection.execute("INSERT INTO checkpoint_threads(thread_id,conversation_id,answer_id) SELECT %s,m.conversation_id,m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=%s AND m.conversation_id=%s AND m.status='generating' AND NOT c.deleting ON CONFLICT(thread_id) DO UPDATE SET answer_id=EXCLUDED.answer_id RETURNING thread_id", (thread_id, answer_id, conversation_id)).fetchone()
            if not saved:
                raise ExecutionPaused()
