"""验证本机模型配置持久化以及密钥不经读取接口返回。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.auth.support import admin_client


def test_settings_survive_restart_without_exposing_secret(database_url: str, tmp_path: Path) -> None:
    """保存配置后可跨重启读取公开字段，空密钥可保留同一服务的已有凭据。"""
    with admin_client(database_url, tmp_path) as client:
        response = client.put("/api/settings", json={
            "base_url": "https://model.example/v1", "model": "chat-model", "api_key": "test-secret-123",
        })
        assert response.status_code == 200
        assert "test-secret-123" not in response.text
    with admin_client(database_url, tmp_path) as restarted:
        settings = restarted.get("/api/settings").json()
        assert settings == {"base_url": "https://model.example/v1", "model": "chat-model", "api_key_configured": True}
        changed = restarted.put("/api/settings", json={"base_url": settings["base_url"], "model": "new-model", "api_key": ""})
        assert changed.status_code == 200
        assert changed.json()["api_key_configured"] is True


@pytest.mark.parametrize("url", ["not-a-url", "http://cloud.example/v1", "https://user:password@cloud.example/v1", "https://cloud.example/v1?key=secret"])
def test_invalid_configuration_is_rejected_without_echoing_secret(database_url: str, tmp_path: Path, url: str) -> None:
    """无效或会暴露凭据的地址不得保存，验证失败也不回显请求中的密钥。"""
    with admin_client(database_url, tmp_path) as client:
        response = client.put("/api/settings", json={"base_url": url, "model": "chat", "api_key": "validation-secret"})
        assert response.status_code == 422
        assert "validation-secret" not in response.text
        assert client.get("/api/settings").json()["api_key_configured"] is False


def test_schema_validation_does_not_echo_secret(database_url: str, tmp_path: Path) -> None:
    """缺少字段的验证错误不得把包含密钥的原始请求体放入响应。"""
    with admin_client(database_url, tmp_path) as client:
        response = client.put("/api/settings", json={"api_key": "validation-secret"})
        assert response.status_code == 422
        assert "validation-secret" not in response.text
