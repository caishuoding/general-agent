"""同步传输入口复用聊天动作编排，云请求期间不持有会话行锁。"""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.attachments import AttachmentContext, AttachmentService
from general_agent.database import Database
from general_agent.budget import BudgetPaused, OperationContext
from general_agent.calls import CallGateway
from general_agent.execution import ChatExecution
from general_agent.provider import ModelFailure, ModelMessage
from general_agent.settings import StoredSettings
from general_agent.summary import DocumentSummary, SummaryPreparation, summary_mode
from general_agent.search import SearchAwaiting


class ChatService:
    """同步调用与流式入口共享明确保存规则和原用户来源。"""

    def __init__(self, database: Database, model: CallGateway, execution: ChatExecution, attachments: AttachmentService) -> None:
        """绑定持久化、统一调用、共同动作与独立会话资料模块。"""
        self.database, self.model, self.execution = database, model, execution
        self.attachments = attachments
        self.documents = DocumentSummary(database, model, attachments)

    def send(self, conversation_id: UUID, content: str, settings: StoredSettings, operation: OperationContext, attachment_ids: list[UUID] | None = None) -> None:
        """先处理明确联网；普通资料先核验能力，全文逐段处理并保留暂停检查点。"""
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
        summary: SummaryPreparation | None = None
        try:
            response = self.execution.prepare(conversation_id,user_id,content,operation,reserved)
            if response is None:
                web_material = self.execution.search.prepare(conversation_id,user_id,content,settings,operation)
                chat_reserved = self.execution.chat_reservation(content,reserved)
                if web_material is None:
                    material = self.attachments.prepare(conversation_id,user_id,content,settings=settings) if summary_mode(content) is None else AttachmentContext()
                    memory_material = self.execution.prepare_answer(user_id,content,operation,reserved)
                    summary = self.documents.prepare(conversation_id,user_id,answer_id,content,settings,operation,chat_reserved,memory_material)
                    if summary:
                        material = summary.material
                else:
                    memory_material = web_material
                history: list[ModelMessage] = [{'role':str(row['role']),'content':str(row['content'])} for row in (rows[-1:] if summary else rows)]
                response = summary.cached_answer if summary and summary.cached_answer else self.model.bind(operation).complete(settings,memory_material + material.messages + history,self.documents.reserve_final(summary,settings,operation) if summary else chat_reserved,answer_id).content
                if summary and summary.cached_answer is None:
                    response = self.documents.finish(summary,response)
        except (BudgetPaused,SearchAwaiting) as paused:
            with self.database.connect() as connection:
                connection.execute("UPDATE messages SET status='paused',budget_pause=%s WHERE id=%s AND status='generating'", (Jsonb(paused.public()) if isinstance(paused,BudgetPaused) else None,answer_id))
            return
        except Exception as error:
            failure = error if isinstance(error,ModelFailure) else ModelFailure(str(error) if isinstance(error,ValueError) else '记忆操作未完成，可以重试核对。')
            response = str(failure)
            self.execution.search.fail(operation,response)
        if failure and summary:
            self.documents.failed_final(summary)
        response, web_sources = self.execution.search.resolve(operation,response)
        response, citations = self.attachments.resolve(conversation_id, response, material)
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET content=%s,status=%s,error=%s,citations=%s,web_sources=%s WHERE id=%s AND status='generating'", (response,'failed' if failure else 'complete',str(failure) if failure else None,Jsonb(citations),Jsonb(web_sources),answer_id))
            connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'", (reserved,))
        if failure:
            raise failure
