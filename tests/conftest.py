"""使用独立的真实 PostgreSQL 数据库隔离每个应用接口测试。"""

import os
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo


@pytest.fixture
def database_url() -> Iterator[str]:
    """创建随机测试数据库，仅清理本测试创建的数据库，不触碰用户业务库。"""
    admin_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://general_agent:local-development-only@127.0.0.1:15432/postgres",
    )
    name = "general_agent_test_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(admin_url, dbname=name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
