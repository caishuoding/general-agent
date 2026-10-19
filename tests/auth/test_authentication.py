"""验证登录、会话、强制改密和初始管理员来源限制。"""

from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from general_agent.app import create_app
from general_agent.auth.api import COOKIE_NAME
from general_agent.auth.passwords import hash_password


def test_bootstrap_admin_forces_password_change_and_blocks_public_login(
    database_url: str, tmp_path: Path
) -> None:
    """初始管理员必须本机登录并先改密，完成后旧会话失效。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        wrong = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert wrong.status_code == 401
        assert wrong.json()["detail"] == "用户名或密码错误。"

        public = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert public.status_code == 403

        local = client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
        assert local.status_code == 200
        assert local.json()["must_change_password"] is True
        old_token = client.cookies.get(COOKIE_NAME)
        assert old_token

        assert client.get("/api/auth/me").status_code == 200
        blocked = client.get("/api/conversations")
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "请先修改密码。"

        weak = client.post(
            "/api/auth/password",
            json={"current_password": "123456", "new_password": "short"},
        )
        assert weak.status_code == 422

        changed = client.post(
            "/api/auth/password",
            json={"current_password": "123456", "new_password": "new-strong-pass"},
        )
        assert changed.status_code == 200
        assert changed.json()["must_change_password"] is False
        assert client.get("/api/conversations").status_code == 200

        client.cookies.set(COOKIE_NAME, old_token)
        assert client.get("/api/auth/me").status_code == 401


def test_logout_expiry_and_disabled_account(database_url: str, tmp_path: Path) -> None:
    """退出、过期会话和停用账号都不能继续访问业务接口。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
        assert login.status_code == 200
        token = client.cookies.get(COOKIE_NAME)
        assert token

        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            connection.execute(
                "UPDATE sessions SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'"
            )
        assert client.get("/api/auth/me").status_code == 401

        client.cookies.set(COOKIE_NAME, token)
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            connection.execute(
                "UPDATE users SET status = 'disabled' WHERE username = 'admin'"
            )
        assert client.post("/api/auth/login", json={"username": "admin", "password": "123456"}).status_code == 401


def test_non_bootstrap_user_can_login_public_and_must_change_password(
    database_url: str, tmp_path: Path
) -> None:
    """管理员创建的用户可以从公网登录，但仍必须先修改临时密码。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000"):
        pass
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute(
            "INSERT INTO users (id, username, password_hash, role, status, must_change_password) "
            "VALUES (%s, 'normal_user', %s, 'user', 'active', TRUE)",
            (uuid4(), hash_password("temporary-pass")),
        )

    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "normal_user", "password": "temporary-pass"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert login.status_code == 200
        assert client.get("/api/conversations").status_code == 403
        changed = client.post(
            "/api/auth/password",
            json={"current_password": "temporary-pass", "new_password": "normal-user-pass"},
        )
        assert changed.status_code == 200
        assert client.get("/api/conversations").status_code == 200
