"""升级前备份并清空旧单用户业务数据。"""

import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

AUTH_UPGRADE_MARKER = "auth_pre_reset_v1"


def run_auth_upgrade(
    database_url: str,
    data_dir: Path,
    clear_memories: Callable[[], None] | None = None,
) -> Path | None:
    """备份旧业务数据后清空一次；已执行过或检测到新账号数据时跳过。"""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        _ensure_schema_migrations(connection)
        if _has_marker(connection):
            return None
        if _has_new_accounts(connection):
            raise RuntimeError("检测到新账号数据，拒绝执行旧数据清空。")

        tables = _business_tables(connection)
        backup_directory = _create_backup(connection, data_dir, tables)
        try:
            if clear_memories is not None:
                clear_memories()
            _clear_attachment_files(data_dir)
            connection.commit()
            with connection.transaction():
                _truncate_tables(connection, tables)
                connection.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")
                connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (AUTH_UPGRADE_MARKER,),
                )
        except Exception:
            raise
        return backup_directory


def _ensure_schema_migrations(connection: psycopg.Connection[dict[str, Any]]) -> None:
    """确保版本表存在，使旧数据库也能记录一次性升级标记。"""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)"
    )
    connection.commit()


def _has_marker(connection: psycopg.Connection[dict[str, Any]]) -> bool:
    """检查一次性清空标记是否已经写入。"""
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE version = %s",
        (AUTH_UPGRADE_MARKER,),
    ).fetchone()
    return row is not None


def _has_new_accounts(connection: psycopg.Connection[dict[str, Any]]) -> bool:
    """检测是否已经进入新账号体系，避免误清空新数据。"""
    exists = connection.execute("SELECT to_regclass('public.users') AS name").fetchone()
    if exists is None or exists["name"] is None:
        return False
    row = connection.execute("SELECT count(*) AS count FROM users").fetchone()
    return bool(row and row["count"])


def _business_tables(connection: psycopg.Connection[dict[str, Any]]) -> list[str]:
    """列出需要清空的业务表，保留版本表和数据库自带对象。"""
    rows = connection.execute(
        "SELECT tablename FROM pg_tables "
        "WHERE schemaname = 'public' AND tablename <> 'schema_migrations' "
        "ORDER BY tablename"
    ).fetchall()
    return [row["tablename"] for row in rows]


def _create_backup(
    connection: psycopg.Connection[dict[str, Any]], data_dir: Path, tables: list[str]
) -> Path:
    """把每张业务表导出为 CSV，并归档附件目录，失败时不返回成功路径。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_directory = data_dir / "backups" / f"auth-{stamp}"
    database_directory = backup_directory / "database"
    try:
        database_directory.mkdir(parents=True)
        manifest: dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": {
                "host": connection.info.host,
                "name": connection.info.dbname,
            },
            "tables": {},
        }
        for table in tables:
            target = database_directory / f"{table}.csv"
            _export_table(connection, table, target)
            count = connection.execute(
                sql.SQL("SELECT count(*) AS count FROM {}").format(sql.Identifier(table))
            ).fetchone()
            manifest["tables"][table] = int(count["count"]) if count else 0
        _archive_attachments(data_dir, backup_directory / "attachments.tar.gz")
        (backup_directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        shutil.rmtree(backup_directory, ignore_errors=True)
        raise
    return backup_directory


def _export_table(
    connection: psycopg.Connection[dict[str, Any]], table: str, target: Path
) -> None:
    """使用 PostgreSQL COPY 把单表导出为带表头的 CSV。"""
    query = sql.SQL("COPY (SELECT * FROM {}) TO STDOUT WITH CSV HEADER").format(
        sql.Identifier(table)
    )
    with connection.cursor() as cursor:
        with target.open("wb") as output:
            with cursor.copy(query) as copy:
                for chunk in copy:
                    output.write(chunk)


def _archive_attachments(data_dir: Path, target: Path) -> None:
    """归档附件目录；目录不存在时仍生成空归档，保持备份结构稳定。"""
    attachments = data_dir / "attachments"
    with tarfile.open(target, "w:gz") as archive:
        if attachments.is_dir():
            for path in sorted(attachments.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=str(path.relative_to(attachments)))


def _clear_attachment_files(data_dir: Path) -> None:
    """删除附件原件；失败时在清空数据库前中止升级。"""
    attachments = data_dir / "attachments"
    if not attachments.exists():
        return
    for path in attachments.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _truncate_tables(
    connection: psycopg.Connection[dict[str, Any]], tables: list[str]
) -> None:
    """一次性截断全部旧业务表，避免外键顺序造成遗漏。"""
    if not tables:
        return
    identifiers = sql.SQL(", ").join(sql.Identifier(table) for table in tables)
    connection.execute(
        sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(identifiers)
    )
