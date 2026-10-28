"""验证旧数据升级时的备份、清空和幂等行为。"""

from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from general_agent.auth.upgrade import AUTH_UPGRADE_MARKER, run_auth_upgrade
from general_agent.infrastructure.database import Database


def apply_migrations_through(connection: psycopg.Connection[dict[str, Any]], last_version: str) -> None:
    """按版本顺序应用指定旧的 SQL 迁移，用于模拟升级前数据库。"""
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
    root = Path(__file__).resolve().parents[2] / "backend" / "general_agent" / "infrastructure" / "migrations"
    for path in sorted(root.glob("*.sql")):
        if path.name > last_version:
            break
        connection.execute(path.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))


def test_upgrade_backs_up_then_clears_legacy_business_data(database_url: str, tmp_path: Path) -> None:
    """升级脚本应保留可恢复备份、清空旧业务数据并记录一次性标记。"""
    attachment_file = tmp_path / "attachments" / "legacy-attachment"
    attachment_file.parent.mkdir(parents=True)
    attachment_file.write_text("旧附件内容", encoding="utf-8")

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        apply_migrations_through(connection, "035_attachment_message_history.sql")
        connection.execute("INSERT INTO conversations (id, title) VALUES (gen_random_uuid(), '旧会话')")

    cleared: list[bool] = []
    backup = run_auth_upgrade(database_url, tmp_path, clear_memories=lambda: cleared.append(True))
    assert backup is not None
    assert cleared == [True]
    manifest_text = (backup / "manifest.json").read_text(encoding="utf-8")
    assert "password" not in manifest_text.lower()
    assert (backup / "manifest.json").exists()
    assert (backup / "database" / "conversations.csv").exists()
    assert (backup / "attachments.tar.gz").exists()
    assert not attachment_file.exists()

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        assert connection.execute("SELECT count(*) AS count FROM conversations").fetchone() == {"count": 0}
        marker = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = %s", (AUTH_UPGRADE_MARKER,)
        ).fetchone()
        assert marker is not None

    assert run_auth_upgrade(database_url, tmp_path) is None
    assert len(list((tmp_path / "backups").iterdir())) == 1

    Database(database_url).migrate()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        assert connection.execute(
                "SELECT count(*) AS count FROM admin_accounts WHERE username = 'admin'"
        ).fetchone() == {"count": 1}


def test_backup_failure_does_not_clear_business_data(database_url: str, tmp_path: Path) -> None:
    """备份目录不可用时升级必须停止，旧会话不能被清空。"""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        apply_migrations_through(connection, "035_attachment_message_history.sql")
        connection.execute("INSERT INTO conversations (id, title) VALUES (gen_random_uuid(), '不能丢失')")

    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        run_auth_upgrade(database_url, blocking_file, clear_memories=lambda: None)

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM conversations WHERE title = '不能丢失'"
        ).fetchone() == {"count": 1}
        marker = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = %s", (AUTH_UPGRADE_MARKER,)
        ).fetchone()
        assert marker is None


def test_memory_clear_failure_does_not_clear_business_tables(
    database_url: str, tmp_path: Path
) -> None:
    """Mem0 清理失败时升级中止，旧业务表保持原样。"""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        apply_migrations_through(connection, "035_attachment_message_history.sql")
        connection.execute("INSERT INTO conversations (id, title) VALUES (gen_random_uuid(), '等待迁移')")

    def fail_memory_clear() -> None:
        """模拟 Mem0 服务不可用。"""
        raise RuntimeError("Mem0 unavailable")

    with pytest.raises(RuntimeError):
        run_auth_upgrade(database_url, tmp_path, clear_memories=fail_memory_clear)

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM conversations WHERE title = '等待迁移'"
        ).fetchone() == {"count": 1}
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE version = %s",
            (AUTH_UPGRADE_MARKER,),
        ).fetchone() is None
