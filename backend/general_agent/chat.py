"""执行文字对话并原子保存消息与调用记录。"""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.attachments import AttachmentService
from general_agent.database import Database
from general_agent.budget import OperationContext
from general_agent.calls import CallGateway
from general_agent.provider import Completion, ModelFailure
from general_agent.settings import StoredSettings


class ChatService:
    """在会话级事务锁下协调上下文、模型请求和结果持久化。"""

    def __init__(self, database: Database, model: CallGateway, attachments: AttachmentService) -> None:
        """绑定持久化、资料模块与统一模型网关。"""
        self.database = database
        self.model = model
        self.attachments = attachments

    def send(self, conversation_id: UUID, content: str, settings: StoredSettings, operation: OperationContext, attachment_ids: list[UUID] | None = None) -> None:
        """校验当前消息附件并串行回答；资料不改变用户指令，失败保存真实状态。"""
        self.attachments.validate(conversation_id, attachment_ids or [])
        material = self.attachments.prepare(conversation_id, None, content, attachment_ids)
        failure: ModelFailure | None = None
        with self.database.connect() as connection:
            conversation = connection.execute("SELECT id FROM conversations WHERE id = %s FOR NO KEY UPDATE", (conversation_id,)).fetchone()
            if conversation is None:
                raise LookupError("会话不存在。")
            if connection.execute("SELECT id FROM messages WHERE conversation_id = %s AND status = 'generating'", (conversation_id,)).fetchone():
                raise ValueError("本会话正在生成回答，请先停止或等待完成。")
            rows = connection.execute(
                "SELECT role, content FROM messages WHERE conversation_id = %s AND status = 'complete' ORDER BY position",
                (conversation_id,),
            ).fetchall()
            context = material.messages + [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]
            context.append({"role": "user", "content": content})
            try:
                response = self.model.bind(operation).complete(settings, context)
            except ModelFailure as error:
                failure = error
                response = Completion(content=str(error), usage=error.usage)
            user_id = uuid4()
            answer, citations = self.attachments.resolve(conversation_id, response.content, material)
            for role, text in (("user", content), ("assistant", answer)):
                connection.execute(
                    "INSERT INTO messages (id, conversation_id, role, content, status, citations) VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_id if role == "user" else uuid4(), conversation_id, role, text, "failed" if failure and role == "assistant" else "complete", Jsonb(citations if role == "assistant" else [])),
                )
            self.attachments.bind(connection, conversation_id, user_id, attachment_ids or [])
            connection.execute("UPDATE conversations SET title = %s WHERE id = %s AND title = '新会话'", (content[:40], conversation_id))
        if failure:
            raise failure
