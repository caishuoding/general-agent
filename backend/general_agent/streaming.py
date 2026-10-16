"""将流式生成进度与每次实际调用独立持久化，浏览器断开不会丢失执行状态。"""

from threading import Lock, Thread
from typing import Any
from uuid import UUID, uuid4

from general_agent.attachments import AttachmentContext, AttachmentService
from general_agent.database import Database
from general_agent.budget import BudgetPaused, OperationContext
from general_agent.execution import ChatExecution
from general_agent.history import HistoryContext
from psycopg.types.json import Jsonb
from general_agent.calls import CallGateway
from general_agent.provider import ModelFailure
from general_agent.settings import StoredSettings
from general_agent.summary import SummaryPreparation
from general_agent.search import SearchAwaiting


class StreamingChat:
    """协调持久化的生成尝试与后台模型请求，每段文字提交后即可重新读取。"""

    def __init__(self, database: Database, model: CallGateway, execution: ChatExecution, attachments: AttachmentService) -> None:
        """绑定共同动作、材料和全文检查点，流式层仅额外跟踪本进程工作线程。"""
        self.database = database
        self.model = model
        self.execution = execution
        self.attachments = attachments
        self.documents = execution.documents
        self.threads: set[Thread] = set()
        self.lock = Lock()

    def recover(self) -> None:
        """恢复中断的回答、文档及自动记忆；预算暂停保留，删除继续保持停用。"""
        with self.database.connect() as connection:
            interrupted = connection.execute("SELECT id,document_job_id FROM messages WHERE status IN ('generating','stopped') AND document_job_id IS NOT NULL").fetchall()
            for message in interrupted:
                connection.execute("UPDATE document_steps SET status='failed' WHERE job_id=%s AND status='running' AND call_id IN (SELECT id FROM model_calls WHERE assistant_message_id=%s)",(message['document_job_id'],message['id']))
                connection.execute('UPDATE document_jobs SET final_call_id=NULL WHERE id=%s AND final_content IS NULL AND final_call_id IN (SELECT id FROM model_calls WHERE assistant_message_id=%s)',(message['document_job_id'],message['id']))
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE,approved=FALSE WHERE id IN (SELECT operation_id FROM messages WHERE status='generating')")
            connection.execute("UPDATE search_requests SET status='failed',error='应用重启中断了搜索，请重新发起。' WHERE operation_id IN (SELECT operation_id FROM messages WHERE status='generating') AND status IN ('planning','approved')")
            connection.execute("UPDATE messages SET status = 'failed', error = '应用重启中断了回答，可以重试。' WHERE status = 'generating'")
            connection.execute("UPDATE model_calls SET status = 'failed',finished_at=CURRENT_TIMESTAMP WHERE status = 'generating'")
            connection.execute("UPDATE model_calls SET finished_at=CURRENT_TIMESTAMP WHERE status = 'stopped' AND finished_at IS NULL")
            connection.execute("UPDATE model_calls SET status = 'cancelled',finished_at=CURRENT_TIMESTAMP WHERE status = 'reserved'")
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE,approved=FALSE WHERE id IN (SELECT operation_id FROM memory_candidate_operations JOIN memory_candidates ON candidate_id=memory_candidates.id WHERE status='saving') OR id IN (SELECT operation_id FROM memory_discoveries WHERE status='pending') AND pause_id IS NULL")
            connection.execute("UPDATE memory_candidates SET status='failed',error='应用重启中断了候选提交，请重试核对已有写入。' WHERE status='saving'")
            connection.execute("UPDATE memory_discoveries SET status='failed',error='应用重启中断了候选识别，可以重试。' WHERE status='pending' AND operation_id IN (SELECT id FROM cloud_operations WHERE cancelled)")
            connection.execute("UPDATE memory_updates SET status='failed',error='应用重启中断了更正，请重试核对已有写入。' WHERE status='saving'")
            connection.execute("UPDATE memory_deletions SET status='failed',error='应用重启中断了清理，请重试；该条目仍停止召回。' WHERE status='pending'")
            connection.execute("UPDATE memory_actions SET status='failed',error='应用重启中断了记忆同步，请重试核对已有写入。' WHERE status='saving'")
            connection.execute("UPDATE memory_native_runs SET status='failed',detail='应用重启中断了自动记忆，可重试原来源；已完成步骤会先核对。' WHERE status='pending' AND operation_id IN (SELECT id FROM cloud_operations WHERE cancelled)")

    def start(self, conversation_id: UUID, content: str, settings: StoredSettings, operation: OperationContext, attachment_ids: list[UUID] | None = None) -> UUID:
        """仅在有效会话校验并绑定附件，预留真实首步后执行共同动作与资料回答。"""
        self.attachments.validate(conversation_id, attachment_ids or [])
        message_id = uuid4()
        with self.database.connect() as connection:
            found = connection.execute("SELECT id FROM conversations WHERE id = %s AND NOT deleting FOR NO KEY UPDATE", (conversation_id,)).fetchone()
            if found is None:
                raise LookupError("会话不存在。")
            if connection.execute("SELECT id FROM messages WHERE conversation_id = %s AND status IN ('generating','paused')", (conversation_id,)).fetchone():
                raise ValueError("本会话正在生成回答，请先停止或等待完成。")
            call_id = self.execution.reserve_first(content, settings, operation)
            user_id = uuid4()
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, status) VALUES (%s, %s, 'user', %s, 'complete')", (user_id, conversation_id, content))
            self.attachments.bind(connection, conversation_id, user_id, attachment_ids or [])
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, status, reply_to, operation_id, model_revision, embedding_revision) VALUES (%s, %s, 'assistant', '', 'generating', %s, %s, %s, %s)", (message_id, conversation_id, user_id, operation.operation_id, settings.revision,self.execution.embedding_revision(content)))
            connection.execute("UPDATE conversations SET title = %s WHERE id = %s AND title = '新会话'", (content[:40], conversation_id))
            connection.execute("UPDATE model_calls SET assistant_message_id=%s WHERE id=%s", (message_id, call_id))
        thread = Thread(target=self.execute, args=(conversation_id, message_id, user_id, settings, operation, call_id), daemon=True)
        with self.lock:
            self.threads = {item for item in self.threads if item.is_alive()}
            self.threads.add(thread)
            thread.start()
        return message_id

    def retry(self, conversation_id: UUID, message_id: UUID, settings: StoredSettings, operation: OperationContext) -> UUID:
        """为最新失败或停止回答创建尝试；来源删除使旧范围永久失效，不能回退到其他附件。"""
        new_id = uuid4()
        with self.database.connect() as connection:
            if not connection.execute("SELECT id FROM conversations WHERE id = %s AND NOT deleting FOR NO KEY UPDATE", (conversation_id,)).fetchone():
                raise LookupError("会话不存在。")
            row = connection.execute("SELECT * FROM messages WHERE conversation_id = %s ORDER BY position DESC LIMIT 1", (conversation_id,)).fetchone()
            if row is None or row["id"] != message_id or row["role"] != "assistant" or row["status"] not in ("failed", "stopped"):
                raise ValueError("只能重试本会话最新一条失败或停止的回答。")
            if connection.execute('SELECT id FROM source_deletions WHERE %s=ANY(operation_ids)',(row['operation_id'],)).fetchone():
                raise ValueError('原来源已删除，旧请求范围已失效；请重新选择资料并发送新消息。')
            user_id = row["reply_to"]
            if user_id is None:
                original = connection.execute("SELECT id FROM messages WHERE conversation_id = %s AND role = 'user' AND position < %s ORDER BY position DESC LIMIT 1", (conversation_id, row["position"])).fetchone()
                if original is None:
                    raise ValueError("无法找到原用户消息。")
                user_id = original["id"]
            source = connection.execute("SELECT content FROM messages WHERE id=%s", (user_id,)).fetchone()
            assert source is not None
            call_id = self.execution.reserve_first(source["content"], settings, operation, user_id)
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, status, reply_to, operation_id, model_revision, embedding_revision) VALUES (%s, %s, 'assistant', '', 'generating', %s, %s, %s, %s)", (new_id, conversation_id, user_id, operation.operation_id, settings.revision,self.execution.embedding_revision(source['content'])))
            connection.execute("UPDATE model_calls SET assistant_message_id=%s WHERE id=%s", (new_id, call_id))
        thread = Thread(target=self.execute, args=(conversation_id, new_id, user_id, settings, operation, call_id), daemon=True)
        with self.lock:
            self.threads = {item for item in self.threads if item.is_alive()}
            self.threads.add(thread)
            thread.start()
        return new_id

    def execute(self, conversation_id: UUID, message_id: UUID, user_id: UUID, settings: StoredSettings, operation: OperationContext, call_id: UUID | None) -> None:
        """共同动作及材料准备后流式输出，保留预算暂停、来源重核和持久化进度。"""
        material = AttachmentContext()
        summary: SummaryPreparation | None = None
        history_material: HistoryContext | None = None
        failure: str | None = None
        try:
            with self.database.connect() as connection:
                rows = connection.execute("SELECT role, content FROM messages WHERE conversation_id = %s AND status = 'complete' AND position <= (SELECT position FROM messages WHERE id = %s) ORDER BY position", (conversation_id, user_id)).fetchall()
            local = self.execution.prepare(conversation_id, user_id, str(rows[-1]['content']), operation, call_id, settings)
            if local is not None:
                with self.database.connect() as connection:
                    connection.execute("UPDATE messages SET content=%s WHERE id=%s AND status='generating'", (local,message_id))
            else:
                prepared=self.execution.prepare_response(conversation_id,user_id,message_id,settings,operation,call_id,rows)
                material,summary,history_material=prepared.material,prepared.summary,prepared.history
                if prepared.local_answer is not None:
                    with self.database.connect() as connection:
                        connection.execute("UPDATE messages SET content=%s WHERE id=%s AND status='generating'", (prepared.local_answer,message_id))
                else:
                    reserved = self.documents.reserve_final(summary,settings,operation) if summary else (prepared.reserved or self.model.budget.reserve(operation,settings))
                    answer = ''
                    for chunk in self.model.bind(operation).stream(settings,prepared.messages,reserved,message_id):
                        answer += chunk.content
                        if chunk.content and (not history_material or self.execution.history.available(operation)):
                            with self.database.connect() as connection:
                                connection.execute("UPDATE messages SET content=content || %s WHERE id=%s AND status='generating'", (chunk.content,message_id))
                    if summary:
                        answer = self.documents.finish(summary,answer)
                        with self.database.connect() as connection:
                            connection.execute("UPDATE messages SET content=%s WHERE id=%s AND status='generating'", (answer,message_id))
        except (BudgetPaused,SearchAwaiting) as paused:
            with self.database.connect() as connection:
                connection.execute("UPDATE messages SET status='paused',budget_pause=%s WHERE id=%s AND status='generating'", (Jsonb(paused.public()) if isinstance(paused,BudgetPaused) else None,message_id))
            return
        except ValueError as error:
            failure = str(error)
        except ModelFailure as error:
            failure = str(error)
        except Exception:
            failure = "回答执行中断，已保留部分内容，可以重试。"
        if failure:
            self.execution.search.fail(operation,failure)
        if failure and summary:
            self.documents.failed_final(summary)
        with self.database.connect() as connection:
            row = connection.execute('SELECT content FROM messages WHERE id=%s',(message_id,)).fetchone()
            if row:
                resolved = self.execution.history.resolve(operation,row['content'], bool(history_material and history_material.messages))
                connection.execute("UPDATE messages SET content=%s WHERE id=%s AND status='generating'",(resolved,message_id))
        self.attachments.save_citations(message_id, conversation_id, material)
        self.execution.search.save_sources(message_id, operation)
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET status = %s, error = %s WHERE id = %s AND status = 'generating'", ("failed" if failure else "complete", failure, message_id))
            connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'", (call_id,))

    def resume(self, conversation_id: UUID, message_id: UUID, settings: StoredSettings) -> None:
        """恢复同一持久化暂停尝试；配置变更后要求显式新尝试以免挪用批准。"""
        with self.database.connect() as connection:
            row = connection.execute("SELECT m.*,o.purpose FROM messages m JOIN cloud_operations o ON o.id=m.operation_id WHERE m.id=%s AND m.conversation_id=%s FOR UPDATE OF m", (message_id,conversation_id)).fetchone()
            if row is None or row['status'] != 'paused':
                raise ValueError('该回答没有等待预算，不能重复恢复。')
            original = connection.execute('SELECT content FROM messages WHERE id=%s',(row['reply_to'],)).fetchone()
            if row['model_revision'] != settings.revision or original is None or not self.execution.same_embedding_revision(row['embedding_revision']):
                raise ValueError('模型或嵌入配置已变更，请停止当前回答后重试。')
            operation = OperationContext(row['operation_id'],row['purpose'],conversation_id)
            connection.execute("UPDATE messages SET status='generating',budget_pause=NULL,error=NULL WHERE id=%s", (message_id,))
        thread = Thread(target=self.execute, args=(conversation_id,message_id,row['reply_to'],settings,operation,None), daemon=True)
        with self.lock:
            self.threads.add(thread)
            thread.start()

    def stop(self, conversation_id: UUID, message_id: UUID) -> None:
        """冻结回答并撤销未用搜索许可；已发出的请求仍可回传真实用量。"""
        message=self.message(conversation_id, message_id)
        if message.get('operation_id'):
            self.execution.search.cancel(message['operation_id'])
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET status = 'stopped',budget_pause=NULL WHERE id = %s AND status IN ('generating','paused')", (message_id,))
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE,approved=FALSE WHERE id IN (SELECT operation_id FROM messages WHERE id=%s)", (message_id,))
            connection.execute("UPDATE model_calls SET status = 'stopped' WHERE assistant_message_id = %s AND status = 'generating' AND EXISTS (SELECT 1 FROM messages WHERE id = %s AND status = 'stopped')", (message_id, message_id))

    def close(self) -> None:
        """停机时终结未完成的回答并短暂等待线程释放，不将中断记录成成功。"""
        self.recover()
        with self.lock:
            threads = tuple(self.threads)
        for thread in threads:
            thread.join(timeout=1)

    def message(self, conversation_id: UUID, message_id: UUID) -> dict[str, Any]:
        """仅返回指定会话中的助手消息，不允许用其他会话标识访问。"""
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM messages WHERE conversation_id = %s AND id = %s AND role = 'assistant'", (conversation_id, message_id)).fetchone()
            if row is None:
                raise LookupError("回答不存在。")
            return self.database.decorate_message(row)
