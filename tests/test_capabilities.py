"""通过公开接口、真实 PostgreSQL 和受控 HTTP 验证各项模型能力。"""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app


def test_independent_embedding_configuration_survives_restart(database_url: str, tmp_path: Path) -> None:
    """嵌入服务独立持久化，复用连接只复制地址与凭据，且不影响会话。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://chat.example/v1", "model": "chat", "api_key": "chat-secret"})
        conversation = client.post("/api/conversations", json={"title": "保留会话"}).json()
        response = client.put("/api/settings/embedding", json={
            "base_url": "https://vectors.example/v1", "model": "embed", "api_key": "embedding-secret", "dimensions": 3,
        })
        assert response.status_code == 200
        assert response.json()["dimensions"] == 3
        assert "secret" not in response.text
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        embedding = client.get("/api/settings/embedding").json()
        assert (embedding["base_url"], embedding["model"], embedding["dimensions"]) == ("https://vectors.example/v1", "embed", 3)
        assert client.get("/api/settings").json()["model"] == "chat"
        assert client.get(f"/api/conversations/{conversation['id']}").json()["title"] == "保留会话"
        copied = client.put("/api/settings/embedding", json={"reuse_chat_connection": True, "model": "another-embed", "dimensions": 4})
        assert copied.status_code == 200
        assert copied.json()["base_url"] == "https://chat.example/v1"
        assert copied.json()["model"] == "another-embed"


def test_capabilities_are_independent_persisted_and_metered(database_url: str, tmp_path: Path) -> None:
    """四项检查分别发出真实协议请求，并持久化部分失败与每次已知用量。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """仅替换云端 HTTP，验证图片与工具确实进入协议并返回混合结果。"""
        payload = json.loads(request.content)
        usage = {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
        if request.url.path == "/v1/embeddings":
            assert request.headers["authorization"] == "Bearer vector-secret"
            assert payload["model"] == "embed"
            return httpx.Response(200, json={"model": "embed", "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}], "usage": {"prompt_tokens": 4, "total_tokens": 4}})
        assert request.headers["authorization"] == "Bearer chat-secret"
        if "tools" in payload:
            return httpx.Response(200, json={"choices": [{"message": {"content": "我不支持工具"}}], "usage": usage})
        if isinstance(payload["messages"][0]["content"], list):
            assert any(part["type"] == "image_url" for part in payload["messages"][0]["content"])
            return httpx.Response(200, json={"choices": [{"message": {"content": "RED"}}], "usage": usage})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "usage": usage})

    transport = httpx.MockTransport(provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://chat.example/v1", "model": "chat", "api_key": "chat-secret"})
        client.put("/api/settings/embedding", json={"base_url": "https://vector.example/v1", "model": "embed", "api_key": "vector-secret", "dimensions": 3})
        for capability, expected in (("text", "passed"), ("image", "passed"), ("tools", "failed"), ("embedding", "passed")):
            response = client.post(f"/api/settings/capabilities/{capability}")
            assert response.status_code == 200
            assert response.json()["status"] == expected
        calls = client.get("/api/usage").json()
        assert len(calls) == 4
        assert sorted(call["purpose"] for call in calls) == ["capability_embedding", "capability_image", "capability_text", "capability_tools"]
        assert sum(call["total_tokens"] for call in calls) == 31
    with TestClient(create_app(database_url, tmp_path, transport), base_url="http://127.0.0.1:8000") as client:
        results = client.get("/api/settings/capabilities").json()
        assert results["text"]["status"] == "passed"
        assert results["image"]["status"] == "passed"
        assert results["tools"]["status"] == "failed"
        assert "工具" in results["tools"]["detail"]
        assert (results["embedding"]["model"], results["embedding"]["dimensions"]) == ("embed", 3)
        client.put("/api/settings", json={"base_url": "https://chat.example/v1", "model": "changed", "api_key": ""})
        invalidated = client.get("/api/settings/capabilities").json()
        assert invalidated["text"]["status"] == "unverified"
        assert invalidated["image"]["status"] == "unverified"
        assert invalidated["tools"]["status"] == "unverified"
        assert invalidated["embedding"]["status"] == "passed"


def test_failed_checks_keep_usage_and_hide_provider_secrets(database_url: str, tmp_path: Path) -> None:
    """认证、错误维度与无效向量分别可解释，服务原文不泄露且失败用量仍被记录。"""
    responses = [
        httpx.Response(401, json={"error": "private-key", "usage": {"prompt_tokens": 2}}),
        httpx.Response(200, json={"model": "embed", "data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 3}}),
        httpx.Response(200, json={"model": "embed", "data": [{"index": 0, "embedding": [True, "0.2", 0.3]}]}),
        httpx.Response(200, json={"model": "another-model", "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}),
    ]
    def provider(request: httpx.Request) -> httpx.Response:
        """依次返回受控服务失败和结构异常，不替换应用内部逻辑。"""
        return responses.pop(0)

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings/embedding", json={"base_url": "https://vector.example/v1", "model": "embed", "api_key": "private-key", "dimensions": 3})
        for expected in ("认证失败", "维度 3", "有限数值", "模型 embed"):
            response = client.post("/api/settings/capabilities/embedding")
            assert response.status_code == 200
            assert response.json()["status"] == "failed"
            assert expected in response.json()["detail"]
            assert "private-key" not in response.text
        calls = client.get("/api/usage").json()
        assert len(calls) == 4
        assert sorted(call["prompt_tokens"] for call in calls if call["prompt_tokens"] is not None) == [2, 3]


def test_late_verification_does_not_validate_changed_credentials(database_url: str, tmp_path: Path) -> None:
    """旧请求晚于密钥修改返回时，界面仍只能看到新配置未验证。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    def provider(request: httpx.Request) -> httpx.Response:
        """在外部请求处等待配置修改，以稳定覆盖旧检查晚到的情况。"""
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://chat.example/v1", "model": "chat", "api_key": "old-key"})
        with ThreadPoolExecutor(max_workers=1) as pool:
            checking = pool.submit(client.post, "/api/settings/capabilities/text")
            assert entered.wait(5)
            try:
                client.put("/api/settings", json={"base_url": "https://chat.example/v1", "model": "chat", "api_key": "new-key"})
            finally:
                release.set()
            assert checking.result().json()["status"] == "passed"
        current = client.get("/api/settings/capabilities").json()["text"]
        assert current["status"] == "unverified"
        assert current["revision"] != checking.result().json()["revision"]


def test_existing_index_blocks_embedding_identity_changes(database_url: str, tmp_path: Path) -> None:
    """已有索引时不能悄悄更换向量空间，但同模型的凭据轮换仍允许。"""
    import psycopg

    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings/embedding", json={"base_url": "https://vector.example/v1", "model": "embed", "api_key": "test-key", "dimensions": 3})
        # 为尚未实现的检索消费者建立已有索引前置状态，断言仍通过应用接口完成。
        with psycopg.connect(database_url) as connection:
            connection.execute("INSERT INTO embedding_indexes (name, base_url, model, dimensions) VALUES ('memory', 'https://vector.example/v1', 'embed', 3)")
        assert client.get("/api/settings/embedding").json()["index_count"] == 1
        for field, value in (("model", "other"), ("dimensions", 4), ("base_url", "https://another.example/v1")):
            body: dict[str, object] = {"base_url": "https://vector.example/v1", "model": "embed", "api_key": "test-key", "dimensions": 3}
            body[field] = value
            blocked = client.put("/api/settings/embedding", json=body)
            assert blocked.status_code == 409
            assert "重建" in blocked.json()["detail"]
            assert client.get("/api/settings/embedding").json()["model"] == "embed"
        rotated = client.put("/api/settings/embedding", json={"base_url": "https://vector.example/v1", "model": "embed", "api_key": "new-key", "dimensions": 3})
        assert rotated.status_code == 200


def test_tool_check_requires_usable_call_and_keeps_unchanged_revision(database_url: str, tmp_path: Path) -> None:
    """工具验证要求可执行的标识及参数；再次保存相同配置不清除已验证结果。"""
    responses = [
        {"type": "function", "function": {"name": "capability_check", "arguments": '{"value":"OK"}'}},
        {"id": "call-1", "type": "function", "function": {"name": "capability_check", "arguments": '{"value":"OK"}'}},
    ]
    def provider(request: httpx.Request) -> httpx.Response:
        """先返回缺少调用标识的结构，再返回完整有效工具调用。"""
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [responses.pop(0)]}}]})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        assert client.post("/api/settings/capabilities/tools").status_code == 409
        assert client.get("/api/usage").json() == []
        body = {"base_url": "https://chat.example/v1", "model": "chat", "api_key": "test-key"}
        client.put("/api/settings", json=body)
        assert client.post("/api/settings/capabilities/tools").json()["status"] == "failed"
        passed = client.post("/api/settings/capabilities/tools").json()
        assert passed["status"] == "passed"
        client.put("/api/settings", json=body)
        assert client.get("/api/settings/capabilities").json()["tools"] == passed


def test_embedding_validation_does_not_echo_credentials(database_url: str, tmp_path: Path) -> None:
    """嵌入配置二次地址校验的错误也必须脱敏，不能回显 URL 凭据或输入密钥。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        for address in ("https://user:url-secret@vector.example/v1", "https://vector.example/v1?key=url-secret", "https://vector.example:wrong/v1"):
            response = client.put("/api/settings/embedding", json={"base_url": address, "model": "embed", "api_key": "body-secret", "dimensions": 3})
            assert response.status_code == 422
            assert "url-secret" not in response.text
            assert "body-secret" not in response.text
            assert client.get("/api/settings/embedding").json()["api_key_configured"] is False
