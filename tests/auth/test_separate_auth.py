"""验证管理端与用户端的独立账号和登录会话。"""

from pathlib import Path

from fastapi.testclient import TestClient

from general_agent.app import create_app
from scripts.recover_admin import recover_admin


def test_admin_and_user_have_independent_login_sessions(database_url: str, tmp_path: Path) -> None:
    """管理员和普通用户使用各自入口登录，输入为两个账号与密码，输出为互不覆盖的会话。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        # 错误入口只返回统一凭据错误，不泄露账号是否属于另一端。
        wrong_entry = client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
        assert wrong_entry.status_code == 401
        assert wrong_entry.json()["detail"] == "用户名或密码错误。"

        # 管理端登录不会让用户端识别为已登录。
        admin = client.post("/api/admin/auth/login", json={"username": "admin", "password": "123456"})
        assert admin.status_code == 200
        assert client.get("/api/admin/auth/me").status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/admin/users").status_code == 403  # 首次登录必须先改密。

        changed = client.post(
            "/api/admin/auth/password",
            json={"current_password": "123456", "new_password": "strong-admin-password"},
        )
        assert changed.status_code == 200
        assert client.get("/api/admin/users").status_code == 200
        assert client.get("/api/auth/me").status_code == 401


def test_admin_manages_only_users_and_reset_password_is_shown_once(database_url: str, tmp_path: Path) -> None:
    """唯一管理员只能创建普通用户，重置输出临时密码一次并使旧密码失效。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        client.post("/api/admin/auth/login", json={"username": "admin", "password": "123456"})
        client.post(
            "/api/admin/auth/password",
            json={"current_password": "123456", "new_password": "strong-admin-password"},
        )
        forbidden = client.post(
            "/api/admin/users",
            json={"username": "another_admin", "password": "strong-password", "role": "admin"},
        )
        assert forbidden.status_code == 422
        created = client.post(
            "/api/admin/users", json={"username": "alice", "password": "alice-initial-password"}
        )
        assert created.status_code == 201
        user_id = created.json()["id"]
        assert all(item["username"] != "admin" for item in client.get("/api/admin/users").json())

        reset = client.post(f"/api/admin/users/{user_id}/reset-password")
        assert reset.status_code == 200
        temporary = reset.json()["temporary_password"]
        assert len(temporary) >= 20
        assert "temporary_password" not in client.get(f"/api/admin/users/{user_id}").json()
        assert client.post(
            "/api/auth/login", json={"username": "alice", "password": "alice-initial-password"}
        ).status_code == 401
        assert client.post(
            "/api/auth/login", json={"username": "alice", "password": temporary}
        ).status_code == 200
        assert client.get("/api/admin/auth/me").status_code == 200
        assert client.get("/api/auth/me").status_code == 200


def test_local_admin_recovery_revokes_old_session(database_url: str, tmp_path: Path) -> None:
    """本机恢复命令接收新密码后应撤销旧会话，并允许新密码从管理端登录。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        client.post("/api/admin/auth/login", json={"username": "admin", "password": "123456"})
        client.post(
            "/api/admin/auth/password",
            json={"current_password": "123456", "new_password": "strong-admin-password"},
        )
        assert client.get("/api/admin/auth/me").status_code == 200
        # 通过恢复命令的公开执行入口验证事务效果，交互式密码输入由终端负责。
        recover_admin(database_url, "recovered-admin-password")
        assert client.get("/api/admin/auth/me").status_code == 401
        assert client.post(
            "/api/admin/auth/login", json={"username": "admin", "password": "strong-admin-password"}
        ).status_code == 401
        assert client.post(
            "/api/admin/auth/login", json={"username": "admin", "password": "recovered-admin-password"}
        ).status_code == 200
        assert any(row["action"] == "recover_password" for row in client.get("/api/admin/audit").json())
