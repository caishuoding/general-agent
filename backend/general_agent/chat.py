"""执行文字对话并原子保存消息与调用记录。"""

from uuid import UUID, uuid4

from general_agent.database import Database
from general_agent.budget import OperationContext
from general_agent.calls import CallGateway
from general_agent.provider import Completion, ModelFailure
from general_agent.settings import StoredSettings


class ChatService:
    """在会话级事务锁下协调上下文、模型请求和结果持久化。"""

    def __init__(self, database: Database, model: CallGateway) -> None:
        """绑定持久化与外部模型边界。"""
        self.database = database
        self.model = model

    def send(self, conversation_id: UUID, content: str, settings: StoredSettings, operation: OperationContext) -> None:
        """串行执行同一会话请求并拒绝已有流式生成；失败先保存真实状态。"""
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
            context = [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]
            context.append({"role": "user", "content": content})
            try:
                response = self.model.bind(operation).complete(settings, context)
            except ModelFailure as error:
                failure = error
                response = Completion(content=str(error), usage=error.usage)
            for role, text in (("user", content), ("assistant", response.content)):
                connection.execute(
                    "INSERT INTO messages (id, conversation_id, role, content, status) VALUES (%s, %s, %s, %s, %s)",
                    (uuid4(), conversation_id, role, text, "failed" if failure and role == "assistant" else "complete"),
                )
            connection.execute("UPDATE conversations SET title = %s WHERE id = %s AND title = '新会话'", (content[:40], conversation_id))
        if failure:
            raise failure
