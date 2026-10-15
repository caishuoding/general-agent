"""同步传输入口复用聊天动作编排，云请求期间不持有会话行锁。"""

from uuid import UUID, uuid4

from general_agent.database import Database
from general_agent.budget import BudgetPaused, OperationContext
from psycopg.types.json import Jsonb
from general_agent.calls import CallGateway
from general_agent.execution import ChatExecution
from general_agent.provider import ModelFailure
from general_agent.settings import StoredSettings


class ChatService:
    """同步调用与流式入口共享明确保存规则和原用户来源。"""

    def __init__(self, database: Database, model: CallGateway, execution: ChatExecution) -> None:
        """绑定持久化、调用计量和共同动作模块。"""
        self.database, self.model, self.execution = database, model, execution

    def send(self, conversation_id: UUID, content: str, settings: StoredSettings, operation: OperationContext) -> None:
        """先落请求身份，再执行动作或文字回答；失败保留可查看、可重试状态。"""
        user_id, answer_id = uuid4(), uuid4()
        with self.database.connect() as connection:
            if not connection.execute('SELECT id FROM conversations WHERE id=%s FOR NO KEY UPDATE', (conversation_id,)).fetchone():
                raise LookupError('会话不存在。')
            if connection.execute("SELECT id FROM messages WHERE conversation_id=%s AND status IN ('generating','paused')", (conversation_id,)).fetchone():
                raise ValueError('本会话正在生成回答，请先停止或等待完成。')
            reserved = self.execution.reserve_first(content, settings, operation)
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,status) VALUES(%s,%s,'user',%s,'complete')", (user_id,conversation_id,content))
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,status,reply_to,operation_id,model_revision,embedding_revision) VALUES(%s,%s,'assistant','','generating',%s,%s,%s,%s)", (answer_id,conversation_id,user_id,operation.operation_id,settings.revision,self.execution.embedding_revision(content)))
            connection.execute("UPDATE conversations SET title=%s WHERE id=%s AND title='新会话'", (content[:40],conversation_id))
            rows = connection.execute("SELECT role,content FROM messages WHERE conversation_id=%s AND status='complete' ORDER BY position", (conversation_id,)).fetchall()
        failure: ModelFailure | None = None
        try:
            response = self.execution.prepare(conversation_id,user_id,content,operation,reserved)
            if response is None:
                material = self.execution.prepare_answer(user_id,content,operation,reserved)
                response = self.model.bind(operation).complete(settings,material+[{'role':str(row['role']),'content':str(row['content'])} for row in rows],self.execution.chat_reservation(content,reserved)).content
        except BudgetPaused as paused:
            with self.database.connect() as connection:
                connection.execute("UPDATE messages SET status='paused',budget_pause=%s WHERE id=%s AND status='generating'", (Jsonb(paused.public()),answer_id))
            return
        except Exception as error:
            failure = error if isinstance(error,ModelFailure) else ModelFailure(str(error) if isinstance(error,ValueError) else '记忆操作未完成，可以重试核对。')
            response = str(failure)
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET content=%s,status=%s,error=%s WHERE id=%s AND status='generating'", (response,'failed' if failure else 'complete',str(failure) if failure else None,answer_id))
            connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'", (reserved,))
        if failure:
            raise failure
