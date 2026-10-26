"""管理 PostgreSQL 结构迁移与会话持久化。"""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row

from general_agent.auth.passwords import BOOTSTRAP_ADMIN_PASSWORD, hash_password


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
                    connection.execute(path.read_text(encoding="utf-8"))
                    connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            self.ensure_bootstrap_admin(connection)

    def ensure_bootstrap_admin(self, connection: psycopg.Connection[dict[str, Any]]) -> None:
        """幂等预置必须改密的初始管理员，已有账号时保持原密码不变。"""
        existing = connection.execute(
            "SELECT id FROM users WHERE username = %s", ("admin",)
        ).fetchone()
        if existing is not None:
            return
        connection.execute(
            "INSERT INTO users (id, username, password_hash, role, status, must_change_password) "
            "VALUES (%s, %s, %s, 'admin', 'active', TRUE)",
            (uuid4(), "admin", hash_password(BOOTSTRAP_ADMIN_PASSWORD)),
        )

    def create_conversation(self, title: str, user_id: UUID) -> dict[str, Any]:
        """为指定账号创建会话并返回其持久化后的标识与时间。"""
        with self.connect() as connection:
            row = connection.execute(
                "INSERT INTO conversations (id, title, user_id) VALUES (%s, %s, %s) RETURNING *",
                (uuid4(), title, user_id),
            ).fetchone()
            assert row is not None
            return row

    def list_conversations(self, user_id: UUID) -> list[dict[str, Any]]:
        """按创建时间倒序列出指定账号仍有效的会话。"""
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM conversations WHERE user_id = %s AND NOT deleting "
                "ORDER BY created_at DESC, id",
                (user_id,),
            ).fetchall()

    def get_conversation(self, conversation_id: UUID, user_id: UUID) -> dict[str, Any] | None:
        """只读取指定账号未删除会话及其消息；其他账号的会话视为不存在。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = %s AND user_id = %s AND NOT deleting",
                (conversation_id, user_id),
            ).fetchone()
            if row is not None:
                row["messages"] = [self.decorate_message(message) for message in connection.execute("SELECT * FROM messages WHERE conversation_id = %s ORDER BY position", (conversation_id,)).fetchall()]
            return row

    def decorate_message(self, row: dict[str, Any]) -> dict[str, Any]:
        """补齐记忆、来源、附件及子任务简要状态，不返回子任务正文或私有推理。"""
        with self.connect() as connection:
            row['agent_tasks'] = connection.execute('SELECT DISTINCT ON (a.id) a.id AS agent_id,a.title,r.id AS run_id,r.status,r.result,r.error FROM agents a JOIN agent_runs r ON r.agent_id=a.id WHERE a.answer_attempt_id=%s AND a.parent_agent_id IS NOT NULL ORDER BY a.id,r.sequence DESC', (row.get('id'),)).fetchall() if row['role'] == 'assistant' else []
            row['attachments'] = connection.execute(
                "SELECT id,filename,size,status,error FROM attachments "
                "WHERE user_message_id=%s ORDER BY created_at,id",
                (row.get('id'),),
            ).fetchall() if row['role'] == 'user' else []
        if row['role'] == 'assistant' and row.get('reply_to'):
            with self.connect() as connection:
                row['memory_action'] = connection.execute('SELECT * FROM memory_actions WHERE id=%s', (uuid5(row['reply_to'],'explicit-memory:0'),)).fetchone()
        else:
            row['memory_action'] = None
        with self.connect() as connection:
            row['automatic_memory'] = connection.execute('SELECT status,detail FROM memory_native_runs WHERE message_id=%s',(row.get('reply_to'),)).fetchone() if row['role']=='assistant' else None
            row['memory_update'] = connection.execute('SELECT * FROM memory_updates WHERE id=%s',(uuid5(row['reply_to'],'explicit-correction:0'),)).fetchone() if row['role']=='assistant' and row.get('reply_to') else None
            if row['role'] == 'assistant' and row.get('reply_to') and row['memory_update'] is None:
                # 工具图中的更正使用内容指纹身份；界面仍按本条来源展示最近一次真实更正。
                row['memory_update'] = connection.execute(
                    'SELECT * FROM memory_updates WHERE source_message_id=%s ORDER BY created_at DESC,id DESC LIMIT 1',
                    (row['reply_to'],),
                ).fetchone()
            row['search_action'] = connection.execute('SELECT id,query,status,error,sources FROM search_requests WHERE operation_id=%s',(row.get('operation_id'),)).fetchone()
            row['memory_recall'] = connection.execute('SELECT status,match_count AS count,detail FROM memory_recalls WHERE operation_id=%s',(row.get('operation_id'),)).fetchone()
        return row

    def list_usage(self, user_id: UUID) -> list[dict[str, Any]]:
        """返回指定账号最近的调用记录，未取得的用量保留为空值。"""
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM model_calls WHERE user_id = %s "
                "AND status NOT IN ('reserved','cancelled') ORDER BY created_at DESC, id LIMIT 100",
                (user_id,),
            ).fetchall()
