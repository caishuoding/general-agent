"""将流式生成进度与每次实际调用独立持久化，浏览器断开不会丢失执行状态。"""

from threading import Lock, Thread
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.attachments.service import AttachmentService
from general_agent.agent.conversation import ConversationContext, ConversationGraph
from general_agent.conversation.progress import PREPARING_PROGRESS
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.checkpoints import STATE_VERSION, supersede_interrupted
from general_agent.infrastructure.operations import OperationContext
from general_agent.conversation.execution import ChatExecution
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.settings import StoredSettings
from general_agent.skills.selection import SkillSnapshot


class StreamingChat:
    """协调持久化的生成尝试与后台模型请求，每段文字提交后即可重新读取。"""

    def __init__(self, database: Database, model: CallGateway, execution: ChatExecution, attachments: AttachmentService, graph: ConversationGraph) -> None:
        """绑定共同动作和材料，流式层仅额外跟踪本进程工作线程。"""
        self.database = database
        self.model = model
        self.execution = execution
        self.attachments = attachments
        self.graph = graph
        self.threads: set[Thread] = set()
        self.lock = Lock()

    def recover(self) -> None:
        """重启保留可续接回答及执行身份，遗留外部请求只结算为中断。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET status='interrupted',error='应用重启中断了执行，可从保存的位置继续。',progress=NULL,interrupted_at=coalesce(interrupted_at,CURRENT_TIMESTAMP) WHERE status='generating' AND resume_model_revision IS NOT NULL")
            connection.execute("UPDATE agent_runs r SET paused_at=coalesce(paused_at,m.interrupted_at) FROM agents a,messages m WHERE r.agent_id=a.id AND a.answer_attempt_id=m.id AND m.status='interrupted' AND r.status IN ('queued','running')")
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE WHERE id IN (SELECT operation_id FROM messages WHERE status='generating')")
            connection.execute("UPDATE messages SET status='failed',error='旧版本执行没有检查点，请重试。',progress=NULL WHERE status='generating'")
            connection.execute("UPDATE model_calls SET status = 'failed',finished_at=CURRENT_TIMESTAMP WHERE status = 'generating'")
            connection.execute("UPDATE model_calls SET finished_at=CURRENT_TIMESTAMP WHERE status = 'stopped' AND finished_at IS NULL")
            connection.execute("UPDATE model_calls SET status = 'cancelled',finished_at=CURRENT_TIMESTAMP WHERE status = 'reserved'")
            connection.execute("UPDATE memory_updates SET status='failed',error='应用重启中断了更正，请重试核对已有写入。' WHERE status='saving'")
            connection.execute("UPDATE memory_deletions SET status='failed',error='应用重启中断了清理，请重试；该条目仍停止召回。' WHERE status='pending'")
            connection.execute("UPDATE memory_actions SET status='failed',error='应用重启中断了记忆同步，请重试核对已有写入。' WHERE status='saving'")
            connection.execute("UPDATE memory_native_runs SET status='failed',detail='应用重启中断了自动记忆，可重试原来源；已完成步骤会先核对。' WHERE status='pending' AND operation_id IN (SELECT id FROM cloud_operations WHERE cancelled)")

    def start(self, conversation_id: UUID, account_id: UUID, content: str, settings: StoredSettings, operation: OperationContext,
              skill_selections: list[SkillSnapshot] | None = None) -> UUID:
        """登记原文、技能快照及模型版本，启动可检查点续接的回答。"""
        message_id = uuid4()
        with self.database.connect() as connection:
            found = connection.execute("SELECT id FROM conversations WHERE id = %s AND user_id = %s AND NOT deleting FOR NO KEY UPDATE", (conversation_id, account_id)).fetchone()
            if found is None:
                raise LookupError("会话不存在。")
            if connection.execute("SELECT id FROM messages WHERE conversation_id = %s AND status='generating'", (conversation_id,)).fetchone():
                raise ValueError("本会话正在生成回答，请先停止或等待完成。")
            supersede_interrupted(connection, conversation_id)
            call_id = self.execution.reserve_first(content, settings, operation)
            user_id = uuid4()
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, status,skill_selections) VALUES (%s, %s, 'user', %s, 'complete',%s)", (user_id, conversation_id, content,Jsonb(skill_selections or [])))
            self.attachments.bind_to_user_message(connection, conversation_id, user_id)
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, status, reply_to, operation_id, progress,resume_model_revision) VALUES (%s, %s, 'assistant', '', 'generating', %s, %s, %s,%s)", (message_id, conversation_id, user_id, operation.operation_id, Jsonb(PREPARING_PROGRESS),settings.revision))
            connection.execute("UPDATE conversations SET title = %s WHERE id = %s AND title = '新会话'", (content[:40], conversation_id))
            connection.execute("UPDATE model_calls SET assistant_message_id=%s WHERE id=%s", (message_id, call_id))
        thread = Thread(target=self.execute, args=(conversation_id, account_id, message_id, user_id, settings, operation, call_id), daemon=True)
        with self.lock:
            self.threads = {item for item in self.threads if item.is_alive()}
            self.threads.add(thread)
            thread.start()
        return message_id

    def retry(self, conversation_id: UUID, account_id: UUID, message_id: UUID, settings: StoredSettings, operation: OperationContext) -> UUID:
        """为最新失败、停止或中断回答创建新尝试，原来源删除后不能重试。"""
        new_id = uuid4()
        with self.database.connect() as connection:
            if not connection.execute("SELECT id FROM conversations WHERE id = %s AND user_id = %s AND NOT deleting FOR NO KEY UPDATE", (conversation_id, account_id)).fetchone():
                raise LookupError("会话不存在。")
            row = connection.execute("SELECT * FROM messages WHERE conversation_id = %s ORDER BY position DESC LIMIT 1", (conversation_id,)).fetchone()
            if row is None or row["id"] != message_id or row["role"] != "assistant" or row["status"] not in ("failed", "stopped", "interrupted"):
                raise ValueError("只能重试本会话最新一条失败、停止或中断的回答。")
            if connection.execute('SELECT id FROM source_deletions WHERE %s=ANY(operation_ids)',(row['operation_id'],)).fetchone():
                raise ValueError('原来源已删除，旧请求范围已失效；请重新选择资料并发送新消息。')
            supersede_interrupted(connection, conversation_id)
            user_id = row["reply_to"]
            if user_id is None:
                original = connection.execute("SELECT id FROM messages WHERE conversation_id = %s AND role = 'user' AND position < %s ORDER BY position DESC LIMIT 1", (conversation_id, row["position"])).fetchone()
                if original is None:
                    raise ValueError("无法找到原用户消息。")
                user_id = original["id"]
            source = connection.execute("SELECT content FROM messages WHERE id=%s", (user_id,)).fetchone()
            assert source is not None
            call_id = self.execution.reserve_first(source["content"], settings, operation, user_id)
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, status, reply_to, operation_id, progress,resume_model_revision) VALUES (%s, %s, 'assistant', '', 'generating', %s, %s, %s,%s)", (new_id, conversation_id, user_id, operation.operation_id, Jsonb(PREPARING_PROGRESS),settings.revision))
            connection.execute("UPDATE model_calls SET assistant_message_id=%s WHERE id=%s", (new_id, call_id))
        thread = Thread(target=self.execute, args=(conversation_id, account_id, new_id, user_id, settings, operation, call_id), daemon=True)
        with self.lock:
            self.threads = {item for item in self.threads if item.is_alive()}
            self.threads.add(thread)
            thread.start()
        return new_id

    def execute(self, conversation_id: UUID, account_id: UUID, message_id: UUID, user_id: UUID, settings: StoredSettings, operation: OperationContext, call_id: UUID | None) -> None:
        """读取带位置的原消息，恢复工作上下文时按水位合入新增历史。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,position,role, content,skill_selections FROM messages WHERE conversation_id = %s AND status = 'complete' AND position <= (SELECT position FROM messages WHERE id = %s) ORDER BY position",
                (conversation_id, user_id),
            ).fetchall()
        context = ConversationContext(
            conversation_id=conversation_id,
            account_id=account_id,
            user_message_id=user_id,
            assistant_message_id=message_id,
            settings=settings,
            operation=operation,
            delivery="stream",
        )
        self.graph.run(context, rows, call_id)

    def resume(self, conversation_id: UUID, account_id: UUID, message_id: UUID, settings: StoredSettings) -> None:
        """重新认领最新中断回答，沿用原操作、调用额度和检查点，不创建用户消息。"""
        current = self.message(conversation_id, account_id, message_id)
        if current['status'] == 'generating':
            return
        checkpoints = self.graph.checkpoints
        if checkpoints is None:
            raise ValueError('当前服务没有启用检查点。')
        with self.database.connect() as connection:
            conversation = connection.execute('SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting FOR UPDATE', (conversation_id, account_id)).fetchone()
            if conversation is None:
                raise LookupError('会话不存在。')
            with checkpoints.conversation(conversation_id):
                row = connection.execute('SELECT * FROM messages WHERE conversation_id=%s ORDER BY position DESC LIMIT 1', (conversation_id,)).fetchone()
                if not row or row['id'] != message_id or row['status'] not in ('interrupted', 'generating'):
                    raise ValueError('只能继续本会话最新的中断回答。')
                if row['status'] == 'generating':
                    return
                if row['resume_model_revision'] != settings.revision:
                    raise ValueError('模型配置已变化，请恢复原配置或重试创建新的回答。')
                saved = checkpoints.values(str(conversation_id))
                if saved and (saved.get('version') != STATE_VERSION or saved.get('answer_id') != str(message_id)):
                    raise ValueError('检查点版本或回答身份不匹配，请重试。')
                root = connection.execute('SELECT * FROM agents WHERE answer_attempt_id=%s AND parent_agent_id IS NULL', (message_id,)).fetchone()
                repository = self.execution.agent_runtime.repository
                if root and root['source_snapshot'] != repository.snapshot(conversation_id):
                    raise ValueError('资料范围已变化，请重新提问。')
                if root and root['deadline'] <= row['interrupted_at']:
                    raise ValueError('原执行在中断前已超时，请重试。')
                with self.database.connect() as state_connection:
                    skill_states = state_connection.execute("SELECT s.payload FROM agent_messages s JOIN agent_runs r ON r.id=s.run_id JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=%s AND a.parent_agent_id IS NULL AND s.step_key='runtime:skill'", (message_id,)).fetchall()
                skills = self.execution.agent_runtime.skills
                for state in skill_states:
                    if skills and state['payload'].get('task_id'):
                        skills.service.instructions(UUID(state['payload']['task_id']), account_id, conversation_id)
                operation = connection.execute('SELECT * FROM cloud_operations WHERE id=%s', (row['operation_id'],)).fetchone()
                if not operation or operation['cancelled'] or operation['invalidated']:
                    raise ValueError('原操作已停止或来源已失效，请重新提问。')
                connection.execute("UPDATE agents SET deadline=deadline+(CURRENT_TIMESTAMP-%s),closed=FALSE WHERE answer_attempt_id=%s", (row['interrupted_at'], message_id))
                connection.execute("UPDATE agent_runs r SET deadline=r.deadline+(CURRENT_TIMESTAMP-coalesce(r.paused_at,%s)),paused_at=NULL,status=CASE WHEN a.parent_agent_id IS NOT NULL THEN 'queued' ELSE 'running' END FROM agents a WHERE r.agent_id=a.id AND a.answer_attempt_id=%s AND (a.parent_agent_id IS NULL OR r.status IN ('queued','running'))", (row['interrupted_at'], message_id))
                connection.execute("UPDATE messages SET status='generating',error=NULL,interrupted_at=NULL,progress=%s WHERE id=%s", (Jsonb(PREPARING_PROGRESS), message_id))
                context = ConversationContext(conversation_id, account_id, row['reply_to'], message_id, settings,
                    OperationContext(operation['id'], operation['purpose'], conversation_id, account_id), 'stream')
        thread = Thread(target=self.continue_answer, args=(context, bool(saved)), daemon=True)
        with self.lock:
            self.threads = {item for item in self.threads if item.is_alive()}
            self.threads.add(thread)
            thread.start()

    def continue_answer(self, context: ConversationContext, has_checkpoint: bool) -> None:
        """有检查点时空输入续接；首次节点前中断则从原用户消息初始化。"""
        if has_checkpoint:
            self.graph.invoke(None, context)
        else:
            self.execute(context.conversation_id, context.account_id, context.assistant_message_id,
                context.user_message_id, context.settings, context.operation, None)

    def stop(self, conversation_id: UUID, account_id: UUID, message_id: UUID) -> None:
        """显式停止生成或中断回答并关闭子任务，已外发调用仍可回传真实用量。"""
        message=self.message(conversation_id, account_id, message_id)
        if message.get('operation_id'):
            self.execution.search.cancel(message['operation_id'])
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET status = 'stopped', progress=NULL WHERE id = %s AND status IN ('generating','interrupted')", (message_id,))
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE WHERE id IN (SELECT operation_id FROM messages WHERE id=%s)", (message_id,))
            connection.execute("UPDATE model_calls SET status = 'stopped' WHERE assistant_message_id = %s AND status = 'generating' AND EXISTS (SELECT 1 FROM messages WHERE id = %s AND status = 'stopped')", (message_id, message_id))

        self.execution.agent_runtime.repository.close(message_id)

    def close(self) -> None:
        """停机将活动回答转为可续接状态，有界等待旧线程退出。"""
        self.recover()
        with self.lock:
            threads = tuple(self.threads)
        for thread in threads:
            thread.join(timeout=2)

    def message(self, conversation_id: UUID, account_id: UUID, message_id: UUID) -> dict[str, Any]:
        """仅返回指定会话中的助手消息，不允许用其他会话标识访问。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                "WHERE m.conversation_id = %s AND m.id = %s AND m.role = 'assistant' "
                "AND c.user_id = %s",
                (conversation_id, message_id, account_id),
            ).fetchone()
            if row is None:
                raise LookupError("回答不存在。")
            return self.database.decorate_message(row)
