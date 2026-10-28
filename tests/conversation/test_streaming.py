"""通过真实 PostgreSQL 和模型 HTTP 边界验证流式执行的持久化状态。"""

import json
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from typing import Any

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from general_agent.conversation.progress import connection_retry_progress, tool_progress
from tests.auth.support import authenticated_client, set_global_settings


class ControlledStream(httpx.SyncByteStream):
    """在真实外部流边界提供可暂停的数据，允许测试观察进行中的回答。"""

    def __init__(self, release: Event, interrupted: bool = False) -> None:
        """记录继续输出开关以及是否模拟没有完成标记的断流。"""
        self.release = release
        self.interrupted = interrupted

    def __iter__(self) -> Iterator[bytes]:
        """先发送第一段，等待测试放行后返回剩余内容与用量。"""
        yield b'data: {"choices":[{"delta":{"content":"first "}}]}\n\n'
        assert self.release.wait(10)
        yield b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":5,"total_tokens":17}}\n\n'
        if self.interrupted:
            return
        yield b'data: {"choices":[{"delta":{"content":"second"},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'


def configure(client: TestClient) -> str:
    """创建使用全局受控模型配置的独立会话。"""
    return str(client.post("/api/conversations", json={}).json()["id"])


def configure_global_model(database_url: str, tmp_path: Path) -> None:
    """通过管理员入口写入测试使用的全局模型配置。"""
    set_global_settings(
        database_url,
        tmp_path,
        {"base_url": "https://model.example/v1", "model": "controlled", "api_key": "test-key"},
    )


def wait_message(client: TestClient, conversation_id: str, status: str, content: str | None = None,
                 timeout: float = 5) -> dict[str, Any]:
    """按会话、目标状态和超时轮询回答；返回公开消息，超时时附上当前状态。"""
    deadline = monotonic() + timeout
    detail: dict[str, Any] = {}
    while monotonic() < deadline:
        detail = client.get(f"/api/conversations/{conversation_id}").json()
        if detail["messages"]:
            message = detail["messages"][-1]
            if message["status"] == status and (content is None or message["content"] == content):
                return dict(message)
        sleep(0.02)
    raise AssertionError(detail)


def test_stream_is_visible_before_completion_and_survives_restart(database_url: str, tmp_path: Path) -> None:
    """模型尚未完成时即可读取部分回答，完成事件、用量与重启后的记录一致。"""
    release = Event()

    def provider(request: httpx.Request) -> httpx.Response:
        """验证使用流式协议，并在外部 HTTP 传输返回分段响应。"""
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["parallel_tool_calls"] is True
        assert len([item for item in payload["messages"] if item["role"] == "system"]) == 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ControlledStream(release))

    transport = httpx.MockTransport(provider)
    configure_global_model(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, transport) as client:
        conversation_id = configure(client)
        try:
            started = client.post(f"/api/conversations/{conversation_id}/runs", json={"content": "hello"})
            assert started.status_code == 202
            first = wait_message(client, conversation_id, "generating", "first ")
            call = client.get("/api/usage").json()[0]
            assert call["status"] == "generating"
            assert call["prompt_tokens"] is None
        finally:
            release.set()
        wait_message(client, conversation_id, "complete", "first second")
        events = client.get(f"/api/conversations/{conversation_id}/messages/{first['id']}/events")
        assert '"status":"complete"' in events.text
        call = client.get("/api/usage").json()[0]
        assert (call["status"], call["prompt_tokens"], call["completion_tokens"]) == ("complete", 12, 5)
    with authenticated_client(database_url, tmp_path, transport) as restarted:
        wait_message(restarted, conversation_id, "complete", "first second")


def test_stream_progress_shows_current_stage_and_clears_on_completion(database_url: str, tmp_path: Path) -> None:
    """模型首段返回前展示准备状态，完成后清除临时进度且不保留步骤历史。"""
    release = Event()

    def provider(request: httpx.Request) -> httpx.Response:
        """阻塞外部模型响应，让测试稳定观察回答启动后的准备状态。"""
        assert release.wait(10)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ControlledStream(release))

    configure_global_model(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation_id = configure(client)
        client.post(f"/api/conversations/{conversation_id}/runs", json={"content": "hello"})
        preparing = wait_message(client, conversation_id, "generating", "")
        assert preparing["progress"] == {"code": "preparing", "label": "正在准备回答…"}
        release.set()
        complete = wait_message(client, conversation_id, "complete", "first second")
        assert complete["progress"] is None


def test_only_key_tools_have_user_facing_progress() -> None:
    """知识库、资料和联网工具映射为简短状态，普通工具不产生额外提示。"""
    knowledge = tool_progress("search_knowledge")
    documents = tool_progress("search_documents")
    reading = tool_progress("read_document")
    searching = tool_progress("search_web")
    fetching = tool_progress("fetch_web")
    assert knowledge is not None and knowledge["label"] == "正在检索知识库…"
    assert documents is not None and documents["label"] == "正在检索资料…"
    assert reading is not None and reading["label"] == "正在阅读资料…"
    assert searching is not None and searching["label"] == "正在联网搜索…"
    assert fetching is not None and fetching["label"] == "正在读取网页…"
    assert tool_progress("get_current_time") is None
    reconnecting = connection_retry_progress(1, 3)
    assert reconnecting == {"code": "connection_retrying", "label": "连接中断，正在自动恢复（1/3）…"}


def test_stop_freezes_partial_answer_and_keeps_late_usage(database_url: str, tmp_path: Path) -> None:
    """停止立即持久化部分内容，已发出请求后续返回的用量仍被保留。"""
    release = Event()

    def provider(request: httpx.Request) -> httpx.Response:
        """暂停外部模型，直到接口确认停止后才返回后续内容与用量。"""
        return httpx.Response(200, stream=ControlledStream(release))

    configure_global_model(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation_id = configure(client)
        try:
            client.post(f"/api/conversations/{conversation_id}/runs", json={"content": "hello"})
            first = wait_message(client, conversation_id, "generating", "first ")
            busy = client.post(f"/api/conversations/{conversation_id}/runs", json={"content": "duplicate"})
            assert busy.status_code == 409
            stopped = client.post(f"/api/conversations/{conversation_id}/messages/{first['id']}/stop")
            assert stopped.status_code == 200
            wait_message(client, conversation_id, "stopped", "first ")
            assert client.get("/api/usage").json()[0]["status"] == "stopped"
        finally:
            release.set()
        deadline = monotonic() + 5
        while monotonic() < deadline:
            usage = client.get("/api/usage").json()
            if usage[0]["total_tokens"] == 17:
                break
            sleep(0.02)
        streamed = next(item for item in usage if item["total_tokens"] == 17)
        assert (streamed["status"], streamed["total_tokens"]) == ("stopped", 17)
        wait_message(client, conversation_id, "stopped", "first ")
        assert len(client.get(f"/api/conversations/{conversation_id}").json()["messages"]) == 2


def test_interrupted_answer_automatically_resumes_from_visible_prefix(database_url: str, tmp_path: Path) -> None:
    """断流保存部分内容和独立费用，自动续写时携带前缀且只保留一条用户消息。"""
    release = Event()
    release.set()
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """第一次模拟断流，第二次验证续写上下文并从正文末尾正常完成。"""
        nonlocal attempts
        payload = json.loads(request.content)
        attempts += 1
        dialogue = [message for message in payload["messages"] if message["role"] in ("user", "assistant")]
        if attempts == 1:
            assert dialogue == [{"role": "user", "content": "hello"}]
            return httpx.Response(200, stream=ControlledStream(release, interrupted=True))
        assert dialogue[:2] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "first "},
        ]
        assert dialogue[-1]["role"] == "user" and "不要重复已经输出的内容" in dialogue[-1]["content"]
        body = (
            'data: {"choices":[{"delta":{"content":"second"},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    configure_global_model(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation_id = configure(client)
        client.post(f"/api/conversations/{conversation_id}/runs", json={"content": "hello"})
        complete = wait_message(client, conversation_id, "complete", "first [[STREAM_RESUMED]]second")
        detail = client.get(f"/api/conversations/{conversation_id}").json()
        assert [(item["role"], item["status"]) for item in detail["messages"]] == [("user", "complete"), ("assistant", "complete")]
        calls = [item for item in client.get("/api/usage").json() if item["total_tokens"] is not None]
        assert sorted((call["status"], call["total_tokens"]) for call in calls) == [("complete", 10), ("failed", 17)]
        assert calls[0]["id"] != calls[1]["id"] and attempts == 2


def test_other_send_interfaces_cannot_overlap_live_answer(database_url: str, tmp_path: Path) -> None:
    """兼容的同步发送接口也必须尊重已有生成，避免同一会话上下文交叉。"""
    release = Event()

    def provider(request: httpx.Request) -> httpx.Response:
        """只接受当前流式执行，额外外部调用不会被伪装成成功。"""
        payload = json.loads(request.content)
        if not payload.get("stream"):
            return httpx.Response(500)
        return httpx.Response(200, stream=ControlledStream(release))

    configure_global_model(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation_id = configure(client)
        try:
            client.post(f"/api/conversations/{conversation_id}/runs", json={"content": "hello"})
            wait_message(client, conversation_id, "generating", "first ")
            response = client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "overlap"})
            assert response.status_code == 409
            assert len(client.get(f"/api/conversations/{conversation_id}").json()["messages"]) == 2
        finally:
            release.set()
        wait_message(client, conversation_id, "complete")


def test_incomplete_finish_still_collects_usage_tail(database_url: str, tmp_path: Path) -> None:
    """达到输出上限不是完整回答，但结束原因之后返回的用量仍需保存。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """按服务协议先返回长度限制结束事件，再返回独立的用量尾帧。"""
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":3,"total_tokens":12}}\n\ndata: [DONE]\n\n')

    configure_global_model(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation_id = configure(client)
        client.post(f"/api/conversations/{conversation_id}/runs", json={"content": "hello"})
        wait_message(client, conversation_id, "failed", "partial")
        call = client.get("/api/usage").json()[0]
        assert (call["status"], call["total_tokens"]) == ("failed", 12)


def test_stream_error_keeps_usage_without_exposing_provider_details(database_url: str, tmp_path: Path) -> None:
    """流中错误携带用量时仍记录该用量，服务原始错误和密钥不会展示。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """返回带敏感原始正文和已知用量的错误事件。"""
        return httpx.Response(200, text='data: {"error":"private-provider-detail","usage":{"prompt_tokens":4,"completion_tokens":0,"total_tokens":4}}\n\n')

    configure_global_model(database_url, tmp_path)
    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation_id = configure(client)
        client.post(f"/api/conversations/{conversation_id}/runs", json={"content": "hello"})
        message = wait_message(client, conversation_id, "failed", "")
        assert "private-provider-detail" not in str(message)
        call = client.get("/api/usage").json()[0]
        assert (call["status"], call["total_tokens"]) == ("failed", 4)
