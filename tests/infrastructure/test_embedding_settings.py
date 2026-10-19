"""通过公开接口和真实 PostgreSQL 验证独立嵌入连接配置。"""

from pathlib import Path

from fastapi.testclient import TestClient

from general_agent.app import create_app


def test_independent_embedding_configuration_survives_restart(database_url: str, tmp_path: Path) -> None:
    """嵌入服务独立持久化，复用连接只复制地址与凭据，且保存不发出云端请求。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://chat.example/v1", "model": "chat", "api_key": "chat-secret"})
        conversation = client.post("/api/conversations", json={"title": "保留会话"}).json()
        response = client.put("/api/settings/embedding", json={
            "base_url": "https://vectors.example/v1", "model": "embed", "api_key": "embedding-secret", "dimensions": 3,
        })
        assert response.status_code == 200
        assert response.json()["dimensions"] == 3
        assert "secret" not in response.text
        assert client.get("/api/usage").json() == []
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        embedding = client.get("/api/settings/embedding").json()
        assert (embedding["base_url"], embedding["model"], embedding["dimensions"]) == ("https://vectors.example/v1", "embed", 3)
        assert client.get("/api/settings").json()["model"] == "chat"
        assert client.get(f"/api/conversations/{conversation['id']}").json()["title"] == "保留会话"
        copied = client.put("/api/settings/embedding", json={"reuse_chat_connection": True, "model": "another-embed", "dimensions": 4})
        assert copied.status_code == 200
        assert copied.json()["base_url"] == "https://chat.example/v1"
        assert copied.json()["model"] == "another-embed"
        assert client.get("/api/usage").json() == []


def test_embedding_validation_does_not_echo_credentials(database_url: str, tmp_path: Path) -> None:
    """嵌入配置地址校验的错误不能回显 URL 凭据或输入密钥。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        for address in ("https://user:url-secret@vector.example/v1", "https://vector.example/v1?key=url-secret", "https://vector.example:wrong/v1"):
            response = client.put("/api/settings/embedding", json={"base_url": address, "model": "embed", "api_key": "body-secret", "dimensions": 3})
            assert response.status_code == 422
            assert "url-secret" not in response.text
            assert "body-secret" not in response.text
            assert client.get("/api/settings/embedding").json()["api_key_configured"] is False
