"""验证用户端与管理端分离，以及不同账号的数据隔离。"""

from pathlib import Path

from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.auth.support import TEST_ADMIN_PASSWORD, admin_client, login_and_change_password


def test_users_and_admin_are_separated_and_conversations_are_isolated(
    database_url: str, tmp_path: Path
) -> None:
    """普通用户看不到管理端，管理员不能进入聊天，双方数据仍按账号隔离。"""
    with admin_client(database_url, tmp_path) as admin:
        created = admin.post(
            "/api/admin/users",
            json={"username": "normal_user", "password": "normal-temp-pass", "role": "user"},
        )
        assert created.status_code == 201
        normal_id = created.json()["id"]

        with TestClient(
            create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000"
        ) as normal:
            login_and_change_password(
                normal, "normal_user", "normal-temp-pass", "normal-user-pass-2"
            )
            own = normal.post("/api/conversations", json={"title": "普通用户会话"}).json()
            uploaded = normal.post(
                f"/api/conversations/{own['id']}/attachments",
                files={"files": ("private.txt", b"normal only")},
            ).json()[0]
            assert normal.get("/api/settings").status_code == 403
            assert normal.get("/api/admin/users").status_code == 403

        assert admin.get("/api/conversations").status_code == 403
        assert admin.get("/api/settings").status_code == 200
        admin_view = admin.get(
            f"/api/admin/users/{normal_id}/conversations/{own['id']}"
        )
        assert admin_view.status_code == 200
        assert admin_view.json()["id"] == own["id"]
        download = admin.get(
            f"/api/admin/users/{normal_id}/conversations/{own['id']}"
            f"/attachments/{uploaded['id']}/file"
        )
        assert download.status_code == 200
        assert download.content == b"normal only"

        wrong_entry = admin.post(
            "/api/auth/login",
            json={
                "username": "normal_user",
                "password": "normal-user-pass-2",
                "expected_role": "admin",
            },
        )
        assert wrong_entry.status_code == 403
        assert wrong_entry.json()["detail"] == "该账号不能登录此入口。"

        assert admin.post("/api/auth/logout").status_code == 200
        user_entry = admin.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": TEST_ADMIN_PASSWORD,
                "expected_role": "user",
            },
        )
        assert user_entry.status_code == 403
