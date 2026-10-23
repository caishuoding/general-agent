"""验证遗留字段清理迁移只删除已失去调用方的数据库结构。"""

import psycopg
from psycopg.rows import dict_row

from general_agent.infrastructure.database import Database


def test_obsolete_columns_are_removed_and_caption_is_retained(database_url: str) -> None:
    """迁移删除旧搜索确认、回答版本和图片结构化字段，同时保留图注字段。"""
    Database(database_url).migrate()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        rows = connection.execute(
            "SELECT table_name,column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=ANY(%s)",
            (['search_requests', 'messages', 'document_images'],),
        ).fetchall()
    columns = {(row['table_name'], row['column_name']) for row in rows}
    assert ('search_requests', 'revision') not in columns
    assert ('search_requests', 'purpose') not in columns
    assert ('search_requests', 'expires_at') not in columns
    assert ('messages', 'model_revision') not in columns
    assert ('messages', 'embedding_revision') not in columns
    assert ('document_images', 'ocr_text') not in columns
    assert ('document_images', 'uncertainties') not in columns
    assert ('document_images', 'caption') in columns
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        schema = connection.execute("SELECT nspname FROM pg_namespace WHERE nspname='langgraph'").fetchone()
    assert schema is None
