"""为接口测试提供已登录客户端和账号辅助。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app

TEST_ADMIN_PASSWORD = "test-admin-password"


def login(client: TestClient, username: str, password: str) -> dict[str, Any] | None:
    """尝试登录并返回账号信息，失败时返回空值。"""
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
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
    user = login(client, "admin", "123456")
    if user is not None:
        return complete_forced_change(client, user)
    user = login(client, "admin", TEST_ADMIN_PASSWORD)
    assert user is not None, "管理员登录失败"
    return user


def login_admin_http(client: httpx.Client) -> dict[str, Any]:
    """为独立进程测试登录管理员，必要时完成首次改密。"""
    response = client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
    if response.status_code == 200 and response.json().get("must_change_password"):
        changed = client.post(
            "/api/auth/password",
            json={"current_password": "123456", "new_password": TEST_ADMIN_PASSWORD},
        )
        assert changed.status_code == 200, changed.text
        return cast(dict[str, Any], changed.json())
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": TEST_ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


@contextmanager
def authenticated_client(
    database_url: str,
    data_dir: Path,
    model_transport: httpx.BaseTransport | None = None,
    mem0_transport: httpx.BaseTransport | None = None,
    raise_server_exceptions: bool = True,
) -> Iterator[TestClient]:
    """启动应用并完成管理员登录，供既有业务接口测试复用。"""
    with TestClient(
        create_app(database_url, data_dir, model_transport, mem0_transport),
        base_url="http://127.0.0.1:8000",
        raise_server_exceptions=raise_server_exceptions,
    ) as client:
        login_admin(client)
        yield client
