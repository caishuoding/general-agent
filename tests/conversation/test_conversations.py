"""通过应用 HTTP 接口验证会话的持久化与隔离。"""

from pathlib import Path

from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.auth.support import authenticated_client


def test_conversation_survives_application_restart(database_url: str, tmp_path: Path) -> None:
    """新建会话在应用重新实例化后仍可从列表和详情中访问。"""
    with authenticated_client(database_url, tmp_path) as client:
        response = client.post("/api/conversations", json={"title": "旅行计划"})
        assert response.status_code == 201
        conversation_id = response.json()["id"]
    with authenticated_client(database_url, tmp_path) as restarted:
        conversations = restarted.get("/api/conversations").json()
        assert [(item["id"], item["title"]) for item in conversations] == [(conversation_id, "旅行计划")]
        detail = restarted.get(f"/api/conversations/{conversation_id}")
        assert detail.status_code == 200
        assert detail.json()["messages"] == []
