"""提供同步聊天入口，模型请求期间不持有会话行锁。"""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.agent.runner import AgentRunner
from general_agent.attachments.service import AttachmentService
from general_agent.infrastructure.checkpoints import supersede_interrupted
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelFailure
from general_agent.infrastructure.settings import StoredSettings


class ChatService:
    """登记同步用户消息并直接调用唯一 AgentRunner。"""

    def __init__(self, database: Database, runner: AgentRunner,
                 attachments: AttachmentService) -> None:
        """绑定持久化、Agent 运行器和用户消息附件关联。"""
        self.database = database
        self.runner = runner
        self.attachments = attachments

    def send(self, conversation_id: UUID, account_id: UUID, content: str,
             settings: StoredSettings, operation: OperationContext,
             skill_names: list[str] | None = None) -> None:
        """创建一轮用户和助手消息，并同步等待唯一 Agent 图完成。"""
        user_id, answer_id = uuid4(), uuid4()
        with self.database.connect() as connection:
            if not connection.execute(
                    'SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting '
                    'FOR NO KEY UPDATE', (conversation_id, account_id)).fetchone():
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
                'resume_model_revision) VALUES(%s,%s,\'assistant\',\'\',\'generating\',%s,%s,%s)',
                (answer_id, conversation_id, user_id, operation.operation_id, settings.revision),
            )
            connection.execute(
                "UPDATE conversations SET title=%s WHERE id=%s AND title='新会话'",
                (content[:40], conversation_id),
            )
        result = self.runner.run_answer(
            conversation_id=conversation_id,
            account_id=account_id,
            user_message_id=user_id,
            assistant_message_id=answer_id,
            settings=settings,
            operation=operation,
            delivery='sync',
        )
        if result.error:
            raise ModelFailure(result.error)
