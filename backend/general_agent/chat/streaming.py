"""提供持久化流式聊天入口和本进程执行线程管理。"""

import logging
from collections.abc import Callable
from threading import Lock, Thread
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.agent.runner import AgentRunner
from general_agent.agent.state import STATE_VERSION
from general_agent.attachments.service import AttachmentService
from general_agent.chat.progress import (
    DRAFTING_PROGRESS,
    PREPARING_PROGRESS,
    MessageProgress,
)
from general_agent.infrastructure.checkpoints import (
    ExecutionPaused,
    supersede_interrupted,
)
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.settings import StoredSettings

LOGGER = logging.getLogger(__name__)


class StreamingChat:
    """协调持久化生成尝试与后台 AgentRunner，浏览器断开不丢失执行状态。"""

    def __init__(self, database: Database, runner: AgentRunner,
                 attachments: AttachmentService) -> None:
        """绑定共同运行器、材料服务和本进程线程集合。"""
        self.database = database
        self.runner = runner
        self.attachments = attachments
        self.threads: set[Thread] = set()
        self.lock = Lock()

    def recover(self) -> None:
        """重启冻结当前回答，保留原执行身份与外发许可，等待显式继续。"""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE messages SET status='interrupted',error='应用重启中断了执行，可从保存的位置继续。',"
                "progress=NULL,interrupted_at=coalesce(interrupted_at,execution_heartbeat,created_at) "
                "WHERE status='generating'"
            )
            connection.execute("UPDATE model_calls SET status='failed',finished_at=CURRENT_TIMESTAMP WHERE status='generating'")
            connection.execute("UPDATE model_calls SET finished_at=CURRENT_TIMESTAMP WHERE status='stopped' AND finished_at IS NULL")
            connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE status='reserved'")
            connection.execute("UPDATE memory_updates SET status='failed',error='应用重启中断了更正，请重试核对已有写入。' WHERE status='saving'")
            connection.execute("UPDATE memory_deletions SET status='failed',error='应用重启中断了清理，请重试；该条目仍停止召回。' WHERE status='pending'")
            connection.execute("UPDATE memory_actions SET status='failed',error='应用重启中断了记忆同步，请重试核对已有写入。' WHERE status='saving'")
            connection.execute("UPDATE memory_native_runs SET status='failed',detail='应用重启中断了自动记忆，可重试原来源；已完成步骤会先核对。' WHERE status='pending' AND operation_id IN (SELECT id FROM cloud_operations WHERE cancelled)")

    def start(self, conversation_id: UUID, account_id: UUID, content: str,
              settings: StoredSettings, operation: OperationContext,
              skill_names: list[str] | None = None) -> UUID:
        """登记流式用户消息并在后台启动唯一 Agent 图。"""
        message_id, user_id = uuid4(), uuid4()
        with self.database.connect() as connection:
            found = connection.execute(
                'SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting '
                'FOR NO KEY UPDATE', (conversation_id, account_id)).fetchone()
            if found is None:
                raise LookupError('会话不存在。')
            if connection.execute(
                    "SELECT id FROM messages WHERE conversation_id=%s AND status='generating'",
                    (conversation_id,)).fetchone():
                raise ValueError('本会话正在生成回答，请先停止或等待完成。')
            supersede_interrupted(connection, conversation_id)
            connection.execute(
                "INSERT INTO messages(id,conversation_id,role,content,status,skill_names) "
                "VALUES(%s,%s,'user',%s,'complete',%s)",
                (user_id, conversation_id, content, Jsonb(skill_names or [])),
            )
            if skill_names:
                self.runner.skill_runtime.service.remember_skills(
                    connection, conversation_id, skill_names)
            self.attachments.bind_to_user_message(connection, conversation_id, user_id)
            connection.execute(
                'INSERT INTO messages(id,conversation_id,role,content,status,reply_to,operation_id,'
                'progress,resume_model_revision) VALUES(%s,%s,\'assistant\',\'\',\'generating\','
                '%s,%s,%s,%s)',
                (message_id, conversation_id, user_id, operation.operation_id,
                 Jsonb(PREPARING_PROGRESS), settings.revision),
            )
            connection.execute(
                "UPDATE conversations SET title=%s WHERE id=%s AND title='新会话'",
                (content[:40], conversation_id),
            )
        self._start_thread(conversation_id, account_id, message_id, user_id, settings, operation)
        return message_id

    def retry(self, conversation_id: UUID, account_id: UUID, message_id: UUID,
              settings: StoredSettings, operation: OperationContext) -> UUID:
        """为最新失败、停止或中断回答创建新尝试，不继承未完成图分支。"""
        new_id = uuid4()
        with self.database.connect() as connection:
            if not connection.execute(
                    'SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting '
                    'FOR NO KEY UPDATE', (conversation_id, account_id)).fetchone():
                raise LookupError('会话不存在。')
            row = connection.execute(
                'SELECT * FROM messages WHERE conversation_id=%s ORDER BY position DESC LIMIT 1',
                (conversation_id,)).fetchone()
            if row is None or row['id'] != message_id or row['role'] != 'assistant' or row['status'] not in (
                    'failed', 'stopped', 'interrupted'):
                raise ValueError('只能重试本会话最新一条失败、停止或中断的回答。')
            if connection.execute('SELECT id FROM source_deletions WHERE %s=ANY(operation_ids)',
                                  (row['operation_id'],)).fetchone():
                raise ValueError('原来源已删除，旧请求范围已失效；请重新选择资料并发送新消息。')
            supersede_interrupted(connection, conversation_id)
            user_id = row['reply_to']
            if user_id is None:
                original = connection.execute(
                    "SELECT id FROM messages WHERE conversation_id=%s AND role='user' "
                    'AND position<%s ORDER BY position DESC LIMIT 1',
                    (conversation_id, row['position'])).fetchone()
                if original is None:
                    raise ValueError('无法找到原用户消息。')
                user_id = original['id']
            connection.execute(
                'INSERT INTO messages(id,conversation_id,role,content,status,reply_to,operation_id,'
                'progress,resume_model_revision) VALUES(%s,%s,\'assistant\',\'\',\'generating\','
                '%s,%s,%s,%s)',
                (new_id, conversation_id, user_id, operation.operation_id,
                 Jsonb(PREPARING_PROGRESS), settings.revision),
            )
        self._start_thread(conversation_id, account_id, new_id, user_id, settings, operation)
        return new_id

    def resume(self, conversation_id: UUID, account_id: UUID, message_id: UUID,
               settings: StoredSettings) -> None:
        """重新认领最新中断回答，沿用原操作和原检查点。"""
        current = self.message(conversation_id, account_id, message_id)
        if current['status'] == 'generating':
            return
        checkpoints = self.runner.checkpoints
        if checkpoints is None:
            raise ValueError('当前服务没有启用检查点。')
        with self.database.connect() as connection:
            conversation = connection.execute(
                'SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting FOR UPDATE',
                (conversation_id, account_id)).fetchone()
            if conversation is None:
                raise LookupError('会话不存在。')
            with checkpoints.conversation(conversation_id):
                row = connection.execute(
                    'SELECT * FROM messages WHERE conversation_id=%s ORDER BY position DESC LIMIT 1',
                    (conversation_id,)).fetchone()
                if not row or row['id'] != message_id or row['status'] not in ('interrupted', 'generating'):
                    raise ValueError('只能继续本会话最新的中断回答。')
                if row['status'] == 'generating':
                    return
                saved = checkpoints.values(str(conversation_id))
                if saved and saved.get('version') != STATE_VERSION:
                    raise ValueError('检查点版本不匹配，请重试。')
                owner = connection.execute(
                    'SELECT answer_id FROM checkpoint_threads WHERE thread_id=%s',
                    (str(conversation_id),),
                ).fetchone()
                root_run = connection.execute(
                    'SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id '
                    'WHERE a.answer_attempt_id=%s AND a.parent_agent_id IS NULL '
                    'ORDER BY r.created_at LIMIT 1',
                    (message_id,),
                ).fetchone()
                own_checkpoint = bool(
                    owner and root_run and saved
                    and str(owner['answer_id']) == str(message_id)
                    and str(saved.get('run_id') or '') == str(root_run['id']))
                operation = self.runner.resume_operation(connection, row, account_id, settings)
                connection.execute(
                    'UPDATE messages SET status=\'generating\',error=NULL,interrupted_at=NULL,'
                    'progress=%s WHERE id=%s', (Jsonb(PREPARING_PROGRESS), message_id))
        thread = Thread(
            target=self._continue,
            args=(conversation_id, account_id, cast(UUID, row['reply_to']), message_id,
                  settings, operation, own_checkpoint),
            daemon=True,
        )
        self._track(thread)
        thread.start()

    def stop(self, conversation_id: UUID, account_id: UUID, message_id: UUID) -> None:
        """停止生成、关闭子任务并清理已失效记忆的派生检查点。"""
        message = self.message(conversation_id, account_id, message_id)
        if message.get('operation_id'):
            self.runner.services.search.cancel(message['operation_id'])
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE messages SET status='stopped',progress=NULL WHERE id=%s "
                "AND status IN ('generating','interrupted')", (message_id,))
            connection.execute(
                'UPDATE cloud_operations SET cancelled=TRUE WHERE id IN '
                '(SELECT operation_id FROM messages WHERE id=%s)', (message_id,))
            connection.execute(
                "UPDATE model_calls SET status='stopped' WHERE assistant_message_id=%s "
                "AND status='generating' AND EXISTS "
                "(SELECT id FROM messages WHERE id=%s AND status='stopped')",
                (message_id, message_id),
            )
        try:
            self.runner.repository.close(message_id)
        finally:
            if message.get('reply_to'):
                self._discard_deleted_memory_checkpoint(
                    conversation_id, message['reply_to'], message_id)

    def close(self) -> None:
        """停机冻结活跃回答并在有限时间内等待本进程线程退出。"""
        self.recover()
        with self.lock:
            threads = tuple(self.threads)
        for thread in threads:
            thread.join(timeout=2)

    def message(self, conversation_id: UUID, account_id: UUID, message_id: UUID) -> dict[str, Any]:
        """仅返回指定会话中的助手消息，不允许跨会话访问。"""
        with self.database.connect() as connection:
            row = connection.execute(
                'SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id '
                "WHERE m.conversation_id=%s AND m.id=%s AND m.role='assistant' AND c.user_id=%s",
                (conversation_id, message_id, account_id)).fetchone()
            if row is None:
                raise LookupError('回答不存在。')
            return self.database.decorate_message(row)

    def _start_thread(self, conversation_id: UUID, account_id: UUID, message_id: UUID,
                      user_id: UUID, settings: StoredSettings,
                      operation: OperationContext) -> None:
        """在后台线程运行新主回答，避免流式 HTTP 请求占用工作线程。"""
        thread = Thread(
            target=self._run,
            args=(conversation_id, account_id, message_id, user_id, settings, operation),
            daemon=True,
        )
        self._track(thread)
        thread.start()

    def _run(self, conversation_id: UUID, account_id: UUID, message_id: UUID,
             user_id: UUID, settings: StoredSettings, operation: OperationContext) -> None:
        """调用 AgentRunner 执行流式回答并把公开增量直接写入消息。"""
        draft = {'new_round': False, 'progress': False}
        try:
            self.runner.run_answer(
                conversation_id=conversation_id,
                account_id=account_id,
                user_message_id=user_id,
                assistant_message_id=message_id,
                settings=settings,
                operation=operation,
                delivery='stream',
                on_content=self._content_writer(message_id, draft),
                on_progress=self._progress_writer(message_id),
                on_round_start=self._round_starter(draft),
            )
        except ExecutionPaused:
            LOGGER.info('流式 Agent 执行已暂停 message_id=%s', message_id)
            return
        except Exception:
            LOGGER.exception('流式 Agent 执行失败 message_id=%s', message_id)
            self._fail_unhandled(message_id)

    def _continue(self, conversation_id: UUID, account_id: UUID, user_id: UUID,
                  message_id: UUID, settings: StoredSettings, operation: OperationContext,
                  has_checkpoint: bool) -> None:
        """在检查点存在时继续原图，首次节点前中断则重新建立本轮初始 State。"""
        draft = {'new_round': False, 'progress': False}
        try:
            self.runner.continue_answer(
                conversation_id=conversation_id,
                account_id=account_id,
                user_message_id=user_id,
                assistant_message_id=message_id,
                settings=settings,
                operation=operation,
                delivery='stream',
                has_checkpoint=has_checkpoint,
                on_content=self._content_writer(message_id, draft),
                on_progress=self._progress_writer(message_id),
                on_round_start=self._round_starter(draft),
            )
        except ExecutionPaused:
            LOGGER.info('流式 Agent 恢复已暂停 message_id=%s', message_id)
            return
        except Exception:
            LOGGER.exception('流式 Agent 恢复失败 message_id=%s', message_id)
            self._fail_unhandled(message_id)

    def _fail_unhandled(self, message_id: UUID) -> None:
        """处理启动或恢复边界异常，保留草稿且不覆盖停止、中断或完成终态。"""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE messages SET status='failed',progress=NULL,"
                "error='回答执行中断，已保留部分内容，可以重试。' "
                "WHERE id=%s AND status='generating'", (message_id,),
            )
        self.runner.repository.close(message_id)

    def _content_writer(self, message_id: UUID,
                        draft: dict[str, bool]) -> Callable[[str], None]:
        """返回按模型回合分段追加公开正文的写入器，保留已有进度与确认入口。"""
        def write(text: str) -> None:
            """追加公开分片，每轮首段与已有正文间加空行；终态消息不接受迟到写入。"""
            if not text:
                return
            if not draft['progress']:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE messages SET progress=%s WHERE id=%s AND status='generating'",
                        (Jsonb(DRAFTING_PROGRESS), message_id))
                draft['progress'] = True
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE messages SET content=content || "
                    "CASE WHEN content<>'' THEN %s ELSE '' END || %s "
                    "WHERE id=%s AND status='generating'",
                    ('\n\n' if draft['new_round'] else '', text, message_id))
            draft['new_round'] = False
        return write

    def _progress_writer(self, message_id: UUID) -> Callable[[MessageProgress], None]:
        """返回覆盖生成中回答最新安全进度的写入器。"""
        def write(progress: Any) -> None:
            """把公开进度投影到当前回答消息。"""
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE messages SET progress=%s WHERE id=%s AND status='generating'",
                    (Jsonb(progress), message_id))
        return write

    def _round_starter(self, draft: dict[str, bool]) -> Callable[[], None]:
        """返回标记新回合边界的回调，下一轮首段到达前保持已展示正文。"""
        def start() -> None:
            """仅重置每轮进度标记，分隔符在收到新公开正文时写入。"""
            draft['new_round'] = True
            draft['progress'] = False
        return start

    def _track(self, thread: Thread) -> None:
        """把新线程加入集合并清理已经结束的线程引用。"""
        with self.lock:
            self.threads = {item for item in self.threads if item.is_alive()}
            self.threads.add(thread)

    def _discard_deleted_memory_checkpoint(self, conversation_id: UUID, user_message_id: UUID,
                                           assistant_message_id: UUID) -> None:
        """回答终态后清除已停用记忆对应的派生会话检查点。"""
        try:
            if self.runner.checkpoints and self.runner.services.memory_deletion_invalidates_checkpoint(user_message_id):
                self.runner.checkpoints.discard_answer(conversation_id, assistant_message_id)
        except Exception:
            LOGGER.exception('清理已删除记忆的检查点失败 message_id=%s', assistant_message_id)
