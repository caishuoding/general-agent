"""使用独立的真实 PostgreSQL 数据库隔离每个应用接口测试。"""

import os
from collections.abc import Iterator
from contextlib import suppress
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
import httpx
from psycopg import sql
from psycopg.conninfo import make_conninfo
from general_agent.memory.mem0_adapter import Mem0Client
from general_agent.memory.service import MemoryService
from tests.fake_mem0 import FakeMem0
from tests.model_protocol import StreamingMockTransport


@pytest.fixture
def database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """创建随机测试数据库和 Mem0 用户，仅清理本测试创建的数据。"""
    admin_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://general_agent:local-development-only@127.0.0.1:15432/postgres",
    )
    name = "general_agent_test_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    memory_user = "test-person-" + uuid4().hex
    # 共享 Mem0 服务按用户隔离，随机测试用户避免已有事实改变新增与删除结果。
    monkeypatch.setenv("MEM0_USER_ID", memory_user)
    try:
        yield make_conninfo(admin_url, dbname=name)
    finally:
        with suppress(Exception), Mem0Client() as memory:
            for entry in memory.get_all(memory_user):
                memory.delete(str(entry["id"]))
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(autouse=True)
def fake_mem0(monkeypatch: pytest.MonkeyPatch) -> FakeMem0:
    """默认用官方 REST 契约替身运行应用测试，显式注入传输时仍使用真实客户端。"""
    monkeypatch.setattr(httpx, "MockTransport", StreamingMockTransport)
    fake = FakeMem0()
    original = MemoryService.session

    @contextmanager
    def session(service: MemoryService) -> Iterator[FakeMem0 | Mem0Client]:
        """未注入传输时返回内存服务，注入传输的停服测试继续使用真实 HTTP 契约。"""
        if service.transport is not None:
            with original(service) as client:
                yield client
            return
        yield fake

    monkeypatch.setattr(MemoryService, "session", session)
    return fake
