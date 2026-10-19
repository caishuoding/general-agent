"""验证不同账号的会话、附件和配置权限隔离。"""

from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from general_agent.app import create_app
from general_agent.auth.passwords import hash_password
from tests.auth.support import TEST_ADMIN_PASSWORD, login_admin


def test_users_cannot_read_each_others_conversations_or_attachments(
    database_url: str, tmp_path: Path
) -> None:
    """普通用户访问他人会话、附件和全局配置时都必须被拒绝。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        login_admin(client)
        admin_conversation = client.post("/api/conversations", json={"title": "管理员会话"}).json()["id"]
        uploaded = client.post(
            f"/api/conversations/{admin_conversation}/attachments",
            files={"files": ("admin.txt", b"admin only")},
        ).json()[0]

        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            connection.execute(
                "INSERT INTO users (id, username, password_hash, role, status, must_change_password) "
                "VALUES (%s, 'normal_user', %s, 'user', 'active', TRUE)",
                (uuid4(), hash_password("normal-temp-pass")),
            )

        assert client.post("/api/auth/logout").status_code == 200
        normal_login = client.post(
            "/api/auth/login",
            json={"username": "normal_user", "password": "normal-temp-pass"},
            headers={"X-Forwarded-For": "203.0.113.20"},
        )
        assert normal_login.status_code == 200
        changed = client.post(
            "/api/auth/password",
            json={"current_password": "normal-temp-pass", "new_password": "normal-user-pass-2"},
        )
        assert changed.status_code == 200

        assert client.get("/api/conversations").json() == []
        assert client.get(f"/api/conversations/{admin_conversation}").status_code == 404
        assert client.get(
            f"/api/conversations/{admin_conversation}/attachments"
        ).status_code == 404
        assert client.get(
            f"/api/conversations/{admin_conversation}/attachments/{uploaded['id']}/file"
        ).status_code == 404
        assert client.get("/api/settings").status_code == 403
        assert client.put(
            "/api/settings",
            json={"base_url": "https://model.example/v1", "model": "chat", "api_key": "secret"},
        ).status_code == 403

        own = client.post("/api/conversations", json={"title": "普通用户会话"}).json()
        assert [item["id"] for item in client.get("/api/conversations").json()] == [own["id"]]

        assert client.post("/api/auth/logout").status_code == 200
        admin_login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": TEST_ADMIN_PASSWORD},
        )
        assert admin_login.status_code == 200
        assert client.get(f"/api/conversations/{own['id']}").status_code == 404
