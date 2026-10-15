"""管理 PostgreSQL 结构迁移与会话持久化。"""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row


class Database:
    """封装会话数据访问，所有请求使用独立连接与事务。"""

    def __init__(self, url: str) -> None:
        """保存连接参数，避免在模块导入时访问数据库。"""
        self.url = url

    def connect(self) -> psycopg.Connection[dict[str, Any]]:
        """建立带超时的连接，由调用方上下文管理事务和关闭。"""
        return psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5)

    def migrate(self) -> None:
        """在事务锁保护下执行尚未应用的版本迁移，重启不会重建已有数据。"""
        with self.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(91402701)")
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                exists = connection.execute("SELECT version FROM schema_migrations WHERE version = %s", (path.name,)).fetchone()
                if exists is None:
                    connection.execute(path.read_text())
                    connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))

    def create_conversation(self, title: str) -> dict[str, Any]:
        """创建会话并返回其持久化后的标识与时间。"""
        with self.connect() as connection:
            row = connection.execute(
                "INSERT INTO conversations (id, title) VALUES (%s, %s) RETURNING *",
                (uuid4(), title),
            ).fetchone()
            assert row is not None
            return row

    def list_conversations(self) -> list[dict[str, Any]]:
        """按创建时间倒序列出个人会话。"""
        with self.connect() as connection:
            return connection.execute("SELECT * FROM conversations ORDER BY created_at DESC, id").fetchall()

    def get_conversation(self, conversation_id: UUID) -> dict[str, Any] | None:
        """读取单个会话及有序消息、已发送附件和可信出处，不存在时返回空值。"""
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM conversations WHERE id = %s", (conversation_id,)).fetchone()
            if row is not None:
                row["messages"] = connection.execute("SELECT m.*, ARRAY(SELECT ma.attachment_id FROM message_attachments ma WHERE ma.message_id=m.id ORDER BY ma.attachment_id) AS attachment_ids FROM messages m WHERE conversation_id = %s ORDER BY position", (conversation_id,)).fetchall()
            return row

    def list_usage(self) -> list[dict[str, Any]]:
        """返回最近的调用记录，未取得的用量保留为空值。"""
        with self.connect() as connection:
            return connection.execute("SELECT * FROM model_calls WHERE status NOT IN ('reserved','cancelled') ORDER BY created_at DESC, id LIMIT 100").fetchall()
