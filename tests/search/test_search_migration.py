"""验证单查询搜索记录升级后仍能准确区分缓存与抓取来源。"""

from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def test_multiple_query_migration_preserves_legacy_cache(database_url: str) -> None:
    """升级旧记录时标记真实查询及空结果，抓取来源与失败记录保持原有语义。"""
    migration = (Path(__file__).resolve().parents[2] / 'backend/general_agent/infrastructure/migrations/047_multiple_search_queries.sql')
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute(
            "CREATE TEMP TABLE search_requests (query TEXT, results JSONB, search_completed BOOLEAN)"
        )
        connection.execute(
            "INSERT INTO search_requests VALUES "
            "('旧查询', '[{\"key\":\"W1\",\"url\":\"https://one.example\"},"
            "{\"key\":\"W2\",\"kind\":\"fetch\",\"url\":\"https://one.example\"}]', TRUE),"
            "('空查询', '[]', TRUE), ('失败查询', '[]', FALSE)"
        )
        connection.execute(migration.read_text(encoding='utf-8'))
        rows = connection.execute('SELECT * FROM search_requests ORDER BY query').fetchall()
        by_query = {row['query']: row for row in rows}
        assert by_query['旧查询']['completed_queries'] == ['旧查询']
        assert by_query['旧查询']['results'] == [
            {'key': 'W1', 'url': 'https://one.example', 'query': '旧查询'},
            {'key': 'W2', 'kind': 'fetch', 'url': 'https://one.example'},
        ]
        assert by_query['空查询']['completed_queries'] == ['空查询']
        assert by_query['失败查询']['completed_queries'] == []
        assert all('search_completed' not in row for row in rows)
