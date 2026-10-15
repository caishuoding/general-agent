"""通过应用 HTTP 接口验证会话的持久化与隔离。"""

from pathlib import Path

from fastapi.testclient import TestClient

from general_agent.app import create_app


def test_conversation_survives_application_restart(database_url: str, tmp_path: Path) -> None:
    """新建会话在应用重新实例化后仍可从列表和详情中访问。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/conversations", json={"title": "旅行计划"})
        assert response.status_code == 201
        conversation_id = response.json()["id"]
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as restarted:
        conversations = restarted.get("/api/conversations").json()
        assert [(item["id"], item["title"]) for item in conversations] == [(conversation_id, "旅行计划")]
        detail = restarted.get(f"/api/conversations/{conversation_id}")
        assert detail.status_code == 200
        assert detail.json()["messages"] == []
