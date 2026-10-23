"""同步传输入口复用聊天动作编排，云请求期间不持有会话行锁。"""

from uuid import UUID, uuid4

from general_agent.attachments.service import AttachmentService
from general_agent.agent.conversation import ConversationContext, ConversationGraph
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.calls import CallGateway
from general_agent.conversation.execution import ChatExecution
from general_agent.infrastructure.provider import ModelFailure
from general_agent.infrastructure.settings import StoredSettings


class ChatService:
    """同步调用与流式入口共享明确保存规则和原用户来源。"""

    def __init__(self, database: Database, model: CallGateway, execution: ChatExecution, attachments: AttachmentService, graph: ConversationGraph) -> None:
        """绑定持久化与统一调用，材料编排复用共同执行器。"""
        self.database, self.model, self.execution = database, model, execution
        self.attachments = attachments
        self.graph = graph

    def send(self, conversation_id: UUID, account_id: UUID, content: str, settings: StoredSettings, operation: OperationContext) -> None:
        """登记消息和附件位置，将历史作为初始 State 交给统一回答图。"""
        user_id, answer_id = uuid4(), uuid4()
        with self.database.connect() as connection:
            if not connection.execute('SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting FOR NO KEY UPDATE', (conversation_id, account_id)).fetchone():
                raise LookupError('会话不存在。')
            if connection.execute("SELECT id FROM messages WHERE conversation_id=%s AND status='generating'", (conversation_id,)).fetchone():
                raise ValueError('本会话正在生成回答，请先停止或等待完成。')
            reserved = self.execution.reserve_first(content, settings, operation)
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,status) VALUES(%s,%s,'user',%s,'complete')", (user_id,conversation_id,content))
            self.attachments.bind_to_user_message(connection, conversation_id, user_id)
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,status,reply_to,operation_id) VALUES(%s,%s,'assistant','','generating',%s,%s)", (answer_id,conversation_id,user_id,operation.operation_id))
            connection.execute("UPDATE conversations SET title=%s WHERE id=%s AND title='新会话'", (content[:40],conversation_id))
            rows = connection.execute("SELECT role,content FROM messages WHERE conversation_id=%s AND status='complete' ORDER BY position", (conversation_id,)).fetchall()
        result = self.graph.run(ConversationContext(
            conversation_id=conversation_id,
            account_id=account_id,
            user_message_id=user_id,
            assistant_message_id=answer_id,
            settings=settings,
            operation=operation,
            delivery="sync",
        ), rows, reserved)
        if result["failure"]:
            raise ModelFailure(result["failure"])
