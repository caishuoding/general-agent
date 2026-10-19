"""为接口测试提供已登录客户端和账号辅助。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import psycopg
from fastapi.testclient import TestClient

from general_agent.app import create_app
from general_agent.auth.passwords import hash_password

TEST_ADMIN_PASSWORD = "test-admin-password"
TEST_USER_PASSWORD = "test-user-password"


def login(
    client: TestClient,
    username: str,
    password: str,
    expected_role: str | None = None,
) -> dict[str, Any] | None:
    """尝试登录并返回账号信息，失败时返回空值。"""
    payload: dict[str, str] = {"username": username, "password": password}
    if expected_role is not None:
        payload["expected_role"] = expected_role
    response = client.post(
        "/api/auth/login", json=payload
    )
    return cast(dict[str, Any], response.json()) if response.status_code == 200 else None


def complete_forced_change(client: TestClient, user: dict[str, Any]) -> dict[str, Any]:
    """若账号仍处于强制改密状态，完成一次强密码修改并返回新账号信息。"""
    if not user.get("must_change_password"):
        return user
    response = client.post(
        "/api/auth/password",
        json={
            "current_password": "123456",
            "new_password": TEST_ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def login_admin(client: TestClient) -> dict[str, Any]:
    """登录初始管理员；已改过密码时使用测试专用强密码。"""
    user = login(client, "admin", "123456", "admin")
    if user is not None:
        return complete_forced_change(client, user)
    user = login(client, "admin", TEST_ADMIN_PASSWORD, "admin")
    assert user is not None, "管理员登录失败"
    return user


def login_admin_http(client: httpx.Client) -> dict[str, Any]:
    """为独立进程测试登录管理员，必要时完成首次改密。"""
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "123456", "expected_role": "admin"},
    )
    if response.status_code == 200 and response.json().get("must_change_password"):
        changed = client.post(
            "/api/auth/password",
            json={"current_password": "123456", "new_password": TEST_ADMIN_PASSWORD},
        )
        assert changed.status_code == 200, changed.text
        return cast(dict[str, Any], changed.json())
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": TEST_ADMIN_PASSWORD, "expected_role": "admin"},
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def switch_to_normal_user(
    client: httpx.Client,
    username: str = "test_user",
    temporary_password: str = "normal-temp-pass",
) -> dict[str, Any]:
    """在已登录管理员会话下创建普通用户，切换为普通用户并完成首次改密。"""
    created = client.post(
        "/api/admin/users",
        json={"username": username, "password": temporary_password, "role": "user"},
    )
    assert created.status_code in {201, 422}, created.text
    client.post("/api/auth/logout")
    login_response = client.post(
        "/api/auth/login",
        json={"username": username, "password": temporary_password, "expected_role": "user"},
    )
    current_password = temporary_password
    if login_response.status_code == 401:
        current_password = TEST_USER_PASSWORD
        login_response = client.post(
            "/api/auth/login",
            json={"username": username, "password": TEST_USER_PASSWORD, "expected_role": "user"},
        )
    assert login_response.status_code == 200, login_response.text
    user = cast(dict[str, Any], login_response.json())
    if user.get("must_change_password"):
        changed = client.post(
            "/api/auth/password",
            json={"current_password": current_password, "new_password": TEST_USER_PASSWORD},
        )
        assert changed.status_code == 200, changed.text
        user = cast(dict[str, Any], changed.json())
    return user


def login_and_change_password(
    client: TestClient, username: str, temporary_password: str, new_password: str
) -> dict[str, Any]:
    """登录指定账号并在首次登录时完成强制改密。"""
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": temporary_password, "expected_role": "user"},
    )
    assert response.status_code == 200, response.text
    user = cast(dict[str, Any], response.json())
    if user.get("must_change_password"):
        changed = client.post(
            "/api/auth/password",
            json={"current_password": temporary_password, "new_password": new_password},
        )
        assert changed.status_code == 200, changed.text
        user = cast(dict[str, Any], changed.json())
    return user


def ensure_normal_user(database_url: str, username: str = "test_user") -> None:
    """在测试数据库创建可直接登录的普通用户，供聊天和附件测试使用。"""
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO users (id, username, password_hash, role, status, must_change_password) "
            "VALUES (%s, %s, %s, 'user', 'active', FALSE) ON CONFLICT DO NOTHING",
            (uuid4(), username, hash_password(TEST_USER_PASSWORD)),
        )


def login_user(client: TestClient, username: str = "test_user") -> dict[str, Any]:
    """以普通用户身份登录用户端。"""
    user = login(client, username, TEST_USER_PASSWORD, "user")
    assert user is not None, "普通用户登录失败"
    return user


@contextmanager
def authenticated_client(
    database_url: str,
    data_dir: Path,
    model_transport: httpx.BaseTransport | None = None,
    mem0_transport: httpx.BaseTransport | None = None,
    raise_server_exceptions: bool = True,
) -> Iterator[TestClient]:
    """启动应用并以普通用户登录，供聊天、附件、记忆和搜索测试复用。"""
    with TestClient(
        create_app(database_url, data_dir, model_transport, mem0_transport),
        base_url="http://127.0.0.1:8000",
        raise_server_exceptions=raise_server_exceptions,
    ) as client:
        ensure_normal_user(database_url)
        login_user(client)
        yield client


@contextmanager
def admin_client(
    database_url: str,
    data_dir: Path,
    model_transport: httpx.BaseTransport | None = None,
    mem0_transport: httpx.BaseTransport | None = None,
) -> Iterator[TestClient]:
    """启动应用并以管理员登录，供设置和管理端接口测试复用。"""
    with TestClient(
        create_app(database_url, data_dir, model_transport, mem0_transport),
        base_url="http://127.0.0.1:8000",
    ) as client:
        login_admin(client)
        yield client


def set_global_settings(
    database_url: str,
    data_dir: Path,
    payload: dict[str, Any],
    model_transport: httpx.BaseTransport | None = None,
    mem0_transport: httpx.BaseTransport | None = None,
) -> None:
    """通过短暂的管理员会话保存全局模型配置，供普通用户测试使用。"""
    with admin_client(database_url, data_dir, model_transport, mem0_transport) as client:
        response = client.put("/api/settings", json=payload)
        assert response.status_code == 200, response.text
