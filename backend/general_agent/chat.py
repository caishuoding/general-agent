"""同步传输入口复用聊天动作编排，云请求期间不持有会话行锁。"""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.attachments import AttachmentContext, AttachmentService
from general_agent.database import Database
from general_agent.budget import BudgetPaused, OperationContext
from general_agent.calls import CallGateway
from general_agent.execution import ChatExecution
from general_agent.history import HistoryContext
from general_agent.provider import ModelFailure
from general_agent.settings import StoredSettings
from general_agent.summary import SummaryPreparation
from general_agent.search import SearchAwaiting


class ChatService:
    """同步调用与流式入口共享明确保存规则和原用户来源。"""

    def __init__(self, database: Database, model: CallGateway, execution: ChatExecution, attachments: AttachmentService) -> None:
        """绑定持久化与统一调用，材料编排及全文检查点复用共同执行器。"""
        self.database, self.model, self.execution = database, model, execution
        self.attachments = attachments
        self.documents = execution.documents

    def send(self, conversation_id: UUID, content: str, settings: StoredSettings, operation: OperationContext, attachment_ids: list[UUID] | None = None) -> None:
        """在有效会话登记授权，统一准备回答材料；同步传输独立保存最终答案及真实状态。"""
        self.attachments.validate(conversation_id, attachment_ids or [])
        user_id, answer_id = uuid4(), uuid4()
        with self.database.connect() as connection:
            if not connection.execute('SELECT id FROM conversations WHERE id=%s AND NOT deleting FOR NO KEY UPDATE', (conversation_id,)).fetchone():
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
        history_material: HistoryContext | None = None
        try:
            response = self.execution.prepare(conversation_id,user_id,content,operation,reserved,settings)
            if response is None:
                prepared=self.execution.prepare_response(conversation_id,user_id,answer_id,settings,operation,reserved,rows)
                material,summary,history_material=prepared.material,prepared.summary,prepared.history
                response=prepared.local_answer if prepared.local_answer is not None else self.model.bind(operation).complete(settings,prepared.messages,self.documents.reserve_final(summary,settings,operation) if summary else prepared.reserved,answer_id).content
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
        response = self.execution.history.resolve(operation,response, bool(history_material and history_material.messages))
        response, web_sources = self.execution.search.resolve(operation,response)
        response, citations = self.attachments.resolve(conversation_id, response, material)
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET content=%s,status=%s,error=%s,citations=%s,web_sources=%s WHERE id=%s AND status='generating'", (response,'failed' if failure else 'complete',str(failure) if failure else None,Jsonb(citations),Jsonb(web_sources),answer_id))
            connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'", (reserved,))
        if failure:
            raise failure
