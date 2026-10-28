"""验证登录基础结构与初始管理员。"""

from pathlib import Path

import psycopg
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from general_agent.app import create_app


def test_migration_seeds_bootstrap_admin_and_auth_tables(
    database_url: str, tmp_path: Path
) -> None:
    """应用启动迁移应创建账号结构并预置必须改密的初始管理员。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/health").status_code == 200

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        admin = connection.execute(
            "SELECT username, password_hash, role, status, must_change_password "
            "FROM admin_accounts WHERE username = 'admin'"
        ).fetchone()
        assert admin is not None
        assert admin["role"] == "admin"
        assert admin["status"] == "active"
        assert admin["must_change_password"] is True
        assert admin["password_hash"].startswith("$argon2id$")
        assert PasswordHasher().verify(admin["password_hash"], "123456")

        table_names = {
            row["table_name"]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            ).fetchall()
        }
        assert {"users", "sessions", "admin_accounts", "admin_sessions", "admin_audit_log"} <= table_names

        user_id = connection.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'conversations' "
            "AND column_name = 'user_id'"
        ).fetchone()
        assert user_id == {"data_type": "uuid", "is_nullable": "NO"}
