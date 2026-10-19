"""通过公开接口验证本机应用的浏览器访问边界。"""

from pathlib import Path

from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.auth.support import authenticated_client


def test_external_website_cannot_write_local_configuration(database_url: str, tmp_path: Path) -> None:
    """其他网站的 Origin 不得修改本机模型配置，同源请求仍可使用。"""
    with authenticated_client(database_url, tmp_path) as client:
        payload = {"base_url": "https://model.example/v1", "model": "chat", "api_key": "test-key"}
        blocked = client.put("/api/settings", json=payload, headers={"Origin": "https://unrelated.example"})
        assert blocked.status_code == 403
        allowed = client.put("/api/settings", json=payload, headers={"Origin": "http://127.0.0.1:8000"})
        assert allowed.status_code == 200
