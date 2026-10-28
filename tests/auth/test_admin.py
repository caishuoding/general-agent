"""验证管理员账号管理、只读跨账号查看和审计。"""

import json
from pathlib import Path

import httpx
import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from general_agent.app import create_app
from tests.auth.support import (
    TEST_ADMIN_PASSWORD,
    admin_client,
    login_admin,
    login_and_change_password,
)


def test_admin_creates_disables_resets_and_cannot_create_second_admin(
    database_url: str, tmp_path: Path
) -> None:
    """唯一管理员只管理普通用户，重置密码使旧会话失效。"""
    with admin_client(database_url, tmp_path) as admin:
        admin_id = admin.get("/api/admin/auth/me").json()["id"]
        assert admin.get("/api/admin/users").status_code == 200

        created = admin.post(
            "/api/admin/users",
            json={"username": "alice", "password": "alice-temp-pass"},
        )
        assert created.status_code == 201
        alice_id = created.json()["id"]
        assert created.json()["must_change_password"] is True

        duplicate = admin.post(
            "/api/admin/users",
            json={"username": "alice", "password": "another"},
        )
        assert duplicate.status_code == 422
        invalid = admin.post(
            "/api/admin/users",
            json={"username": "Alice!", "password": "another"},
        )
        assert invalid.status_code == 422

        with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as alice:
            login_and_change_password(alice, "alice", "alice-temp-pass", "alice-strong-pass")
            assert alice.get("/api/admin/users").status_code == 401

            disabled = admin.patch(
                f"/api/admin/users/{alice_id}", json={"status": "disabled"}
            )
            assert disabled.status_code == 200
            assert alice.get("/api/auth/me").status_code == 401
            assert alice.post(
                "/api/auth/login",
                json={"username": "alice", "password": "alice-strong-pass"},
            ).status_code == 401

            enabled = admin.patch(
                f"/api/admin/users/{alice_id}", json={"status": "active"}
            )
            assert enabled.status_code == 200
            assert alice.post(
                "/api/auth/login",
                json={"username": "alice", "password": "alice-strong-pass"},
            ).status_code == 200

            reset = admin.post(
                f"/api/admin/users/{alice_id}/reset-password",
            )
            assert reset.status_code == 200
            assert reset.json()["must_change_password"] is True
            temporary_password = reset.json()["temporary_password"]
            assert alice.get("/api/auth/me").status_code == 401
            relogin = alice.post(
                "/api/auth/login",
                json={"username": "alice", "password": temporary_password},
            )
            assert relogin.status_code == 200
            assert relogin.json()["must_change_password"] is True

        ops = admin.post(
            "/api/admin/users",
            json={"username": "ops_admin", "password": "ops-temp-pass", "role": "admin"},
        )
        assert ops.status_code == 422
        assert admin.patch(
            f"/api/admin/users/{admin_id}", json={"status": "disabled"}
        ).status_code == 404
        assert admin.patch(
            f"/api/admin/users/{admin_id}", json={"role": "user"}
        ).status_code == 422


def test_admin_can_view_all_user_content_read_only_and_audit_it(
    database_url: str, tmp_path: Path
) -> None:
    """管理员可查看目标账号的会话、附件、记忆、搜索和用量，但不能代发消息。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """为普通用户回答返回固定文本。"""
        payload = json.loads(request.content)
        assert payload["messages"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "管理员可见的回答"}}]})

    with admin_client(database_url, tmp_path) as admin:
        admin.put(
            "/api/settings",
            json={"base_url": "https://model.example/v1", "model": "chat", "api_key": "test-key"},
        )
        created = admin.post(
            "/api/admin/users",
            json={"username": "bob", "password": "bob-temp-pass"},
        )
        assert created.status_code == 201
        bob_id = created.json()["id"]

        with TestClient(
            create_app(database_url, tmp_path, httpx.MockTransport(provider)),
            base_url="http://127.0.0.1:8000",
        ) as bob:
            login_and_change_password(bob, "bob", "bob-temp-pass", "bob-strong-pass")
            conversation = bob.post("/api/conversations", json={"title": "Bob 会话"}).json()["id"]
            uploaded = bob.post(
                f"/api/conversations/{conversation}/attachments",
                files={"files": ("bob.txt", b"bob private attachment")},
            ).json()[0]
            for _ in range(50):
                state = bob.get(f"/api/conversations/{conversation}/attachments").json()[0]
                if state["status"] == "ready":
                    break
                import time
                time.sleep(0.02)
            sent = bob.post(
                f"/api/conversations/{conversation}/messages",
                json={"content": "请回答"},
            )
            assert sent.status_code == 200, sent.text

        conversations = admin.get(f"/api/admin/users/{bob_id}/conversations")
        assert conversations.status_code == 200
        assert [item["id"] for item in conversations.json()] == [conversation]
        found = admin.get(f"/api/admin/users/{bob_id}/conversations", params={"query": "请回答"})
        assert found.status_code == 200
        assert found.json()[0]["matched_message_id"] is not None
        assert admin.get(f"/api/admin/users/{bob_id}/conversations", params={"query": "不存在的内容"}).json() == []
        detail = admin.get(
            f"/api/admin/users/{bob_id}/conversations/{conversation}"
        )
        assert detail.status_code == 200
        assert any(item["content"] == "管理员可见的回答" for item in detail.json()["messages"])
        download = admin.get(
            f"/api/admin/users/{bob_id}/conversations/{conversation}"
            f"/attachments/{uploaded['id']}/file"
        )
        assert download.status_code == 200
        assert download.content == b"bob private attachment"
        assert admin.get(
            f"/api/admin/users/{bob_id}/conversations/{conversation}"
            f"/attachments/{uploaded['id']}/preview"
        ).status_code == 415
        with psycopg.connect(database_url) as connection:
            connection.execute(
                "UPDATE attachments SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE id=%s",
                (uploaded["id"],),
            )
        expired_detail = admin.get(f"/api/admin/users/{bob_id}/conversations/{conversation}").json()
        assert all(not message["attachments"] for message in expired_detail["messages"])
        assert admin.get(
            f"/api/admin/users/{bob_id}/conversations/{conversation}"
            f"/attachments/{uploaded['id']}/file"
        ).status_code == 404
        assert admin.get(f"/api/admin/users/{bob_id}/memories").status_code == 200
        assert admin.get(f"/api/admin/users/{bob_id}/search").status_code == 200
        assert admin.get(f"/api/admin/users/{bob_id}/usage").status_code == 200
        assert admin.post(
            f"/api/admin/users/{bob_id}/conversations/{conversation}/messages",
            json={"content": "管理员不能代发"},
        ).status_code in {404, 405}

        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            audit = connection.execute(
                "SELECT admin_user_id, target_user_id, resource_type, action "
                "FROM admin_audit_log WHERE target_user_id=%s ORDER BY created_at",
                (bob_id,),
            ).fetchall()
        assert audit
        assert all(row["admin_user_id"] is not None and row["action"] in {"view", "download"} for row in audit)
        assert any(row["resource_type"] == "conversation" for row in audit)
        visible_audit = admin.get("/api/admin/audit", params={"target_user_id": bob_id, "action": "download"})
        assert visible_audit.status_code == 200
        assert any(row["resource_type"] == "attachment" for row in visible_audit.json())
