"""同步传输入口复用聊天动作编排，云请求期间不持有会话行锁。"""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.attachments import AttachmentContext, AttachmentService
from general_agent.database import Database
from general_agent.budget import BudgetPaused, OperationContext
from general_agent.calls import CallGateway
from general_agent.execution import ChatExecution
from general_agent.provider import ModelFailure
from general_agent.settings import StoredSettings


class ChatService:
    """同步调用与流式入口共享明确保存规则和原用户来源。"""

    def __init__(self, database: Database, model: CallGateway, execution: ChatExecution, attachments: AttachmentService) -> None:
        """绑定持久化、统一调用、共同动作与独立会话资料模块。"""
        self.database, self.model, self.execution = database, model, execution
        self.attachments = attachments

    def send(self, conversation_id: UUID, content: str, settings: StoredSettings, operation: OperationContext, attachment_ids: list[UUID] | None = None) -> None:
        """绑定当前来源并发现待确认候选后准备记忆资料；实际读图才核验能力，暂停保留身份。"""
        self.attachments.validate(conversation_id, attachment_ids or [])
        user_id, answer_id = uuid4(), uuid4()
        with self.database.connect() as connection:
            if not connection.execute('SELECT id FROM conversations WHERE id=%s FOR NO KEY UPDATE', (conversation_id,)).fetchone():
                raise LookupError('会话不存在。')
            if connection.execute("SELECT id FROM messages WHERE conversation_id=%s AND status IN ('generating','paused')", (conversation_id,)).fetchone():
                raise ValueError('本会话正在生成回答，请先停止或等待完成。')
            reserved = self.execution.reserve_first(content, settings, operation)
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,status) VALUES(%s,%s,'user',%s,'complete')", (user_id,conversation_id,content))
            self.attachments.bind(connection, conversation_id, user_id, attachment_ids or [])
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,status,reply_to,operation_id,model_revision,embedding_revision) VALUES(%s,%s,'assistant','','generating',%s,%s,%s,%s)", (answer_id,conversation_id,user_id,operation.operation_id,settings.revision,self.execution.embedding_revision(content)))
            connection.execute("UPDATE conversations SET title=%s WHERE id=%s AND title='新会话'", (content[:40],conversation_id))
            rows = connection.execute("SELECT role,content FROM messages WHERE conversation_id=%s AND status='complete' ORDER BY position", (conversation_id,)).fetchall()
        failure: ModelFailure | None = None
        material = AttachmentContext()
        try:
            response = self.execution.prepare(conversation_id,user_id,content,operation,reserved,settings)
            if response is None:
                material = self.attachments.prepare(conversation_id, user_id, content, settings=settings)
                memory_material = self.execution.prepare_answer(user_id,content,operation,reserved)
                response = self.model.bind(operation).complete(settings,memory_material + material.messages + [{'role':str(row['role']),'content':str(row['content'])} for row in rows],self.execution.chat_reservation(content,reserved)).content
        except BudgetPaused as paused:
            with self.database.connect() as connection:
                connection.execute("UPDATE messages SET status='paused',budget_pause=%s WHERE id=%s AND status='generating'", (Jsonb(paused.public()),answer_id))
            return
        except Exception as error:
            failure = error if isinstance(error,ModelFailure) else ModelFailure(str(error) if isinstance(error,ValueError) else '记忆操作未完成，可以重试核对。')
            response = str(failure)
        response, citations = self.attachments.resolve(conversation_id, response, material)
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET content=%s,status=%s,error=%s,citations=%s WHERE id=%s AND status='generating'", (response,'failed' if failure else 'complete',str(failure) if failure else None,Jsonb(citations),answer_id))
            connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'", (reserved,))
        if failure:
            raise failure
