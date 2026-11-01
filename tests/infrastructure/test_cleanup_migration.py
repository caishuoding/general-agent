"""验证遗留字段清理迁移只删除已失去调用方的数据库结构。"""

from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from general_agent.infrastructure.database import Database


def migrate_through(connection: psycopg.Connection[Any], cutoff: str) -> None:
    """为数据迁移测试只应用指定版本以前的真实项目迁移。"""
    migration_directory = Path(__file__).resolve().parents[2] / 'backend/general_agent/infrastructure/migrations'
    connection.execute('CREATE TABLE schema_migrations (version TEXT PRIMARY KEY)')
    for path in sorted(migration_directory.glob('*.sql')):
        if path.name > cutoff:
            break
        connection.execute(path.read_text(encoding='utf-8'))
        connection.execute('INSERT INTO schema_migrations(version) VALUES(%s)', (path.name,))


def test_obsolete_columns_are_removed_and_caption_is_retained(database_url: str) -> None:
    """迁移删除旧列、旧状态和独立工具图结构，保留当前恢复字段。"""
    Database(database_url).migrate()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=ANY(%s)",
            (['search_requests', 'messages', 'document_images', 'agents'],),
        ).fetchall()
    columns = {(row['table_name'], row['column_name']) for row in rows}
    assert ('search_requests', 'revision') not in columns
    assert ('search_requests', 'purpose') not in columns
    assert ('search_requests', 'expires_at') not in columns
    assert ('messages', 'model_revision') not in columns
    assert ('messages', 'resume_model_revision') in columns
    assert ('messages', 'execution_heartbeat') in columns
    assert ('messages', 'embedding_revision') not in columns
    assert ('document_images', 'ocr_text') not in columns
    assert ('document_images', 'uncertainties') not in columns
    assert ('document_images', 'caption') in columns
    assert ('skill_tasks', 'stage') not in columns
    assert ('skill_tasks', 'route') not in columns
    assert ('agents', 'web_count') not in columns
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        obsolete = connection.execute("SELECT to_regclass('public.memory_tool_runs') AS name").fetchone()
        search_status = connection.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid='search_requests'::regclass AND contype='c' "
            "AND pg_get_constraintdef(oid) LIKE '%%status%%'"
        ).fetchone()
        skill_status = connection.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid='skill_tasks'::regclass AND contype='c' "
            "AND pg_get_constraintdef(oid) LIKE '%%status%%'"
        ).fetchone()
    assert obsolete is not None and obsolete['name'] is None
    assert search_status is not None
    assert not any(status in search_status['definition'] for status in ('planning', 'pending', 'rejected', 'expired'))
    assert skill_status is not None and 'completed' not in skill_status['definition']


def test_cleanup_migrations_convert_existing_rows_before_tightening_constraints(database_url: str) -> None:
    """真实旧行在升级时映射为取消和等待，不能只验证空库的新约束。"""
    account_id, conversation_id, message_id = uuid4(), uuid4(), uuid4()
    with psycopg.connect(database_url) as connection:
        migrate_through(connection, '064_remove_obsolete_skill_stage_route.sql')
        connection.execute(
            "INSERT INTO users(id,username,password_hash,role,status,must_change_password) "
            "VALUES(%s,'migration_user','test','user','active',FALSE)",
            (account_id,),
        )
        connection.execute(
            "INSERT INTO conversations(id,title,user_id) VALUES(%s,'迁移测试',%s)",
            (conversation_id, account_id),
        )
        connection.execute(
            "INSERT INTO messages(id,conversation_id,role,content,status) VALUES(%s,%s,'assistant','', 'complete')",
            (message_id, conversation_id),
        )
        for index, status in enumerate(('planning', 'pending', 'rejected', 'expired')):
            operation_id = uuid4()
            connection.execute(
                "INSERT INTO cloud_operations(id,scope_hash,purpose,conversation_id,user_id) "
                "VALUES(%s,%s,'迁移测试',%s,%s)",
                (operation_id, str(operation_id), conversation_id, account_id),
            )
            connection.execute(
                "INSERT INTO search_requests(id,operation_id,conversation_id,user_id,message_id,query,search_revision,status) "
                "VALUES(%s,%s,%s,%s,%s,%s,'legacy',%s)",
                (uuid4(), operation_id, conversation_id, account_id, message_id, f'query-{index}', status),
            )
        connection.execute(
            "INSERT INTO skill_tasks(id,account_id,conversation_id,skill,version,entry_digest,status) "
            "VALUES(%s,%s,%s,'ppt-master','1','digest','completed')",
            (uuid4(), account_id, conversation_id),
        )
        connection.execute(
            "INSERT INTO agents(id,answer_attempt_id,account_id,conversation_id,title,deadline,web_count) "
            "VALUES(%s,%s,%s,%s,'旧主智能体',CURRENT_TIMESTAMP + INTERVAL '10 minutes',9)",
            (uuid4(), message_id, account_id, conversation_id),
        )

    Database(database_url).migrate()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        search_statuses = connection.execute('SELECT status,error FROM search_requests ORDER BY created_at,id').fetchall()
        skill_status = connection.execute('SELECT status FROM skill_tasks').fetchone()
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='agents'"
        ).fetchall()
    assert [row['status'] for row in search_statuses] == ['cancelled'] * 4
    assert all(row['error'] for row in search_statuses)
    assert skill_status == {'status': 'waiting'}
    assert 'web_count' not in {row['column_name'] for row in columns}
