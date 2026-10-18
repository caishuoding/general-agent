"""以真实数据库和受控外部 HTTP 响应验证文字对话。"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app


def no_tool_action(payload: dict[str, object]) -> httpx.Response | None:
    """为普通聊天的 Agent 规划阶段返回无需工具，随后由测试验证正式回答。"""
    if not payload.get("tools"):
        return None
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "NO_TOOL_ACTION"}}], "usage": {}})


def test_reply_and_usage_survive_restart(database_url: str, tmp_path: Path) -> None:
    """模型回答、消息归属及服务返回用量在重启后仍可通过接口读取。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """在外部模型边界校验请求，返回固定回答与已知用量。"""
        assert request.url == "https://model.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-key"
        payload = json.loads(request.content)
        assert payload["model"] == "chat-model"
        planning = no_tool_action(payload)
        if planning is not None:
            return planning
        dialogue = [message for message in payload["messages"] if message["role"] in ("user", "assistant")]
        assert dialogue == [{"role": "user", "content": "你好"}]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "你好，今天想聊什么？"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        })

    transport = httpx.MockTransport(provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://model.example/v1", "model": "chat-model", "api_key": "fixture-key"})
        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        reply = client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "你好"})
        assert reply.status_code == 200
    with TestClient(create_app(database_url, tmp_path, transport), base_url="http://127.0.0.1:8000") as restarted:
        detail = restarted.get(f"/api/conversations/{conversation_id}").json()
        assert [(message["role"], message["content"], message["status"]) for message in detail["messages"]] == [
            ("user", "你好", "complete"), ("assistant", "你好，今天想聊什么？", "complete"),
        ]
        usage = restarted.get("/api/usage").json()
        answered = next(item for item in usage if item["total_tokens"] == 18)
        assert (answered["prompt_tokens"], answered["completion_tokens"]) == (10, 8)
        assert answered["status"] == "complete"


def test_provider_failure_is_persisted_without_secret(database_url: str, tmp_path: Path) -> None:
    """认证失败应保留用户输入和失败状态，但不回传服务错误中的凭据。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """模拟会在错误正文中回显凭据的外部服务。"""
        return httpx.Response(401, json={"error": "bad fixture-secret-do-not-expose"})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000", raise_server_exceptions=False) as client:
        client.put("/api/settings", json={"base_url": "https://model.example/v1", "model": "chat-model", "api_key": "fixture-secret-do-not-expose"})
        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        response = client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "请帮我写一段开场白"})
        assert response.status_code == 502
        assert "fixture-secret-do-not-expose" not in response.text
        messages = client.get(f"/api/conversations/{conversation_id}").json()["messages"]
        assert messages[0]["content"] == "请帮我写一段开场白"
        assert messages[1]["status"] == "failed"
        assert "认证失败" in messages[1]["content"]
        call = client.get("/api/usage").json()[0]
        assert call["status"] == "failed"
        assert call["prompt_tokens"] is None


def test_http_failure_preserves_returned_usage(database_url: str, tmp_path: Path) -> None:
    """服务以失败状态返回已发生的用量时，调用记录仍保存这些数据。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """模拟已消耗输入 token 后发生的服务错误。"""
        return httpx.Response(500, json={"error": "private-provider-detail", "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://model.example/v1", "model": "chat", "api_key": "test-key"})
        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        response = client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "你好"})
        assert response.status_code == 502
        assert "private-provider-detail" not in response.text
        usage = client.get("/api/usage").json()[0]
        assert usage["status"] == "failed"
        assert (usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]) == (7, 2, 9)


def test_conversations_keep_separate_model_context(database_url: str, tmp_path: Path) -> None:
    """往返两个会话提出普通问题时，模型只收到当前会话的消息；个人事实候选另有专项验证。"""
    expected = [
        [{"role": "user", "content": "介绍一下绿茶"}],
        [{"role": "user", "content": "今天下雨吗"}],
        [{"role": "user", "content": "介绍一下绿茶"}, {"role": "assistant", "content": "已了解本会话的信息。"}, {"role": "user", "content": "继续"}],
    ]

    def provider(request: httpx.Request) -> httpx.Response:
        """依据普通问题的预期上下文检查外部请求，排除候选提取等独立动作。"""
        payload = json.loads(request.content)
        planning = no_tool_action(payload)
        if planning is not None:
            return planning
        dialogue = [message for message in payload["messages"] if message["role"] in ("user", "assistant")]
        assert dialogue == expected.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": "已了解本会话的信息。"}}]})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://model.example/v1", "model": "chat", "api_key": "test-key"})
        first = client.post("/api/conversations", json={}).json()["id"]
        second = client.post("/api/conversations", json={}).json()["id"]
        for conversation_id, content in [(first, "介绍一下绿茶"), (second, "今天下雨吗"), (first, "继续")]:
            assert client.post(f"/api/conversations/{conversation_id}/messages", json={"content": content}).status_code == 200
        assert len(client.get(f"/api/conversations/{first}").json()["messages"]) == 4
        assert len(client.get(f"/api/conversations/{second}").json()["messages"]) == 2
        assert client.get("/api/usage").json()[0]["total_tokens"] is None


@pytest.mark.parametrize("mode", ["timeout", "bad-json", "no-text"])
def test_unusable_response_is_not_a_success(database_url: str, tmp_path: Path, mode: str) -> None:
    """超时或没有有效文字的响应应记为失败，已经返回的用量仍需保留。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """模拟外部服务超时、非 JSON 正文和缺失回答的情况。"""
        if mode == "timeout":
            raise httpx.ReadTimeout("contains-secret", request=request)
        if mode == "bad-json":
            return httpx.Response(200, text="contains-secret")
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 0, "total_tokens": 9}})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://model.example/v1", "model": "chat", "api_key": "test-key"})
        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        response = client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "你好"})
        assert response.status_code == 502
        assert "contains-secret" not in response.text
        call = client.get("/api/usage").json()[0]
        assert call["status"] == "failed"
        assert call["prompt_tokens"] == (9 if mode == "no-text" else None)
