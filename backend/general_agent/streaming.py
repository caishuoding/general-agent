"""将流式生成进度与每次实际调用独立持久化，浏览器断开不会丢失执行状态。"""

from threading import Lock, Thread
from typing import Any
from uuid import UUID, uuid4

from general_agent.database import Database
from general_agent.provider import ModelClient, ModelFailure, TokenUsage
from general_agent.settings import StoredSettings


class StreamingChat:
    """协调持久化的生成尝试与后台模型请求，每段文字提交后即可重新读取。"""

    def __init__(self, database: Database, model: ModelClient) -> None:
        """绑定数据库与模型边界，并跟踪本进程的工作线程。"""
        self.database = database
        self.model = model
        self.threads: set[Thread] = set()
        self.lock = Lock()

    def recover(self) -> None:
        """单进程应用重启时将遗留的生成标为中断，避免永久显示生成中。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET status = 'failed', error = '应用重启中断了回答，可以重试。' WHERE status = 'generating'")
            connection.execute("UPDATE model_calls SET status = 'failed' WHERE status = 'generating'")

    def start(self, conversation_id: UUID, content: str, settings: StoredSettings) -> UUID:
        """先原子保存用户消息和生成记录，随后启动模型请求。"""
        message_id = uuid4()
        with self.database.connect() as connection:
            found = connection.execute("SELECT id FROM conversations WHERE id = %s FOR UPDATE", (conversation_id,)).fetchone()
            if found is None:
                raise LookupError("会话不存在。")
            if connection.execute("SELECT id FROM messages WHERE conversation_id = %s AND status = 'generating'", (conversation_id,)).fetchone():
                raise ValueError("本会话正在生成回答，请先停止或等待完成。")
            user_id = uuid4()
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, status) VALUES (%s, %s, 'user', %s, 'complete')", (user_id, conversation_id, content))
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, status, reply_to) VALUES (%s, %s, 'assistant', '', 'generating', %s)", (message_id, conversation_id, user_id))
            connection.execute("UPDATE conversations SET title = %s WHERE id = %s AND title = '新会话'", (content[:40], conversation_id))
        thread = Thread(target=self.execute, args=(conversation_id, message_id, user_id, settings), daemon=True)
        with self.lock:
            self.threads = {item for item in self.threads if item.is_alive()}
            self.threads.add(thread)
            thread.start()
        return message_id

    def retry(self, conversation_id: UUID, message_id: UUID, settings: StoredSettings) -> UUID:
        """为最新失败或停止的回答创建新尝试，复用原用户消息并保留旧回答。"""
        new_id = uuid4()
        with self.database.connect() as connection:
            if not connection.execute("SELECT id FROM conversations WHERE id = %s FOR UPDATE", (conversation_id,)).fetchone():
                raise LookupError("会话不存在。")
            row = connection.execute("SELECT * FROM messages WHERE conversation_id = %s ORDER BY position DESC LIMIT 1", (conversation_id,)).fetchone()
            if row is None or row["id"] != message_id or row["role"] != "assistant" or row["status"] not in ("failed", "stopped"):
                raise ValueError("只能重试本会话最新一条失败或停止的回答。")
            user_id = row["reply_to"]
            if user_id is None:
                original = connection.execute("SELECT id FROM messages WHERE conversation_id = %s AND role = 'user' AND position < %s ORDER BY position DESC LIMIT 1", (conversation_id, row["position"])).fetchone()
                if original is None:
                    raise ValueError("无法找到原用户消息。")
                user_id = original["id"]
            connection.execute("INSERT INTO messages (id, conversation_id, role, content, status, reply_to) VALUES (%s, %s, 'assistant', '', 'generating', %s)", (new_id, conversation_id, user_id))
        thread = Thread(target=self.execute, args=(conversation_id, new_id, user_id, settings), daemon=True)
        with self.lock:
            self.threads = {item for item in self.threads if item.is_alive()}
            self.threads.add(thread)
            thread.start()
        return new_id

    def execute(self, conversation_id: UUID, message_id: UUID, user_id: UUID, settings: StoredSettings) -> None:
        """消费单次已发出的模型流，提交内容与用量并将失败安全地记录下来。"""
        call_id = uuid4()
        usage = TokenUsage()
        failure: str | None = None
        try:
            with self.database.connect() as connection:
                active = connection.execute("SELECT status FROM messages WHERE id = %s FOR UPDATE", (message_id,)).fetchone()
                if active is None or active["status"] != "generating":
                    return
                rows = connection.execute("SELECT role, content FROM messages WHERE conversation_id = %s AND status = 'complete' AND position <= (SELECT position FROM messages WHERE id = %s) ORDER BY position", (conversation_id, user_id)).fetchall()
                connection.execute("INSERT INTO model_calls (id, conversation_id, assistant_message_id, model, status) VALUES (%s, %s, %s, %s, 'generating')", (call_id, conversation_id, message_id, settings.model))
            for chunk in self.model.stream(settings, [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]):
                if chunk.usage is not None:
                    usage = chunk.usage
                with self.database.connect() as connection:
                    if chunk.content:
                        connection.execute("UPDATE messages SET content = content || %s WHERE id = %s AND status = 'generating'", (chunk.content, message_id))
                    connection.execute("UPDATE model_calls SET prompt_tokens = %s, completion_tokens = %s, total_tokens = %s WHERE id = %s", (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, call_id))
        except ModelFailure as error:
            failure = str(error)
            usage = error.usage
        except Exception:
            failure = "回答执行中断，已保留部分内容，可以重试。"
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET status = %s, error = %s WHERE id = %s AND status = 'generating'", ("failed" if failure else "complete", failure, message_id))
            connection.execute("UPDATE model_calls SET status = (SELECT status FROM messages WHERE id = %s), prompt_tokens = %s, completion_tokens = %s, total_tokens = %s WHERE id = %s", (message_id, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, call_id))

    def stop(self, conversation_id: UUID, message_id: UUID) -> None:
        """冻结正在生成的回答，并保留已发出请求继续回传用量的机会。"""
        self.message(conversation_id, message_id)
        with self.database.connect() as connection:
            connection.execute("UPDATE messages SET status = 'stopped' WHERE id = %s AND status = 'generating'", (message_id,))
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
            return row
