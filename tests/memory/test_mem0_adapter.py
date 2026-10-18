"""验证官方 Mem0 REST API 客户端的请求与错误契约。"""

import json

import httpx
import pytest

from general_agent.memory.mem0_adapter import Mem0ApiError, Mem0Client


def test_mem0_client_uses_official_memory_endpoints() -> None:
    """核对新增、查询、读取、更新和删除均走官方 HTTP 接口及用户范围。"""
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """记录请求并返回各端点的最小官方响应。"""
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        assert request.headers["x-api-key"] == "mem0-secret"
        if request.method == "POST" and request.url.path == "/memories":
            return httpx.Response(200, json={"results": [{"id": "memory-1", "memory": "我喜欢茶"}]})
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"id": "memory-1", "score": 0.9}]})
        if request.method == "GET" and request.url.path == "/memories":
            assert dict(request.url.params) == {"user_id": "local-person", "page_size": "25"}
            return httpx.Response(200, json={"results": [{"id": "memory-1"}]})
        if request.method == "GET":
            return httpx.Response(200, json={"id": "memory-1", "memory": "我喜欢茶"})
        return httpx.Response(200, json={"status": "ok"})

    with Mem0Client("http://mem0.test", "mem0-secret", transport=httpx.MockTransport(provider)) as client:
        added = client.add([{"role": "user", "content": "我喜欢茶"}], "local-person", {"action_id": "action-1"}, infer=False)
        assert client._results(added)[0]["id"] == "memory-1"
        assert client.search("茶", "local-person", limit=3)["results"][0]["score"] == 0.9
        assert client.get_all("local-person", limit=25) == [{"id": "memory-1"}]
        assert client.get("memory-1") == {"id": "memory-1", "memory": "我喜欢茶"}
        client.update("memory-1", "我每天喝茶", {"source": "user"})
        client.delete("memory-1")

    assert requests[0] == (
        "POST",
        "/memories",
        {
            "messages": [{"role": "user", "content": "我喜欢茶"}],
            "user_id": "local-person",
            "infer": False,
            "metadata": {"action_id": "action-1"},
        },
    )
    assert requests[1][2] == {"query": "茶", "user_id": "local-person", "limit": 3}
    assert requests[4][2] == {"text": "我每天喝茶", "memory": "我每天喝茶", "metadata": {"source": "user"}}


def test_mem0_client_normalizes_not_found_and_errors() -> None:
    """单条不存在返回空值，服务失败与无效 JSON 转换成统一诊断异常。"""
    responses = iter([
        httpx.Response(404, json={"detail": "not found"}),
        httpx.Response(503, text="service unavailable"),
        httpx.Response(200, text="not-json"),
    ])

    def provider(request: httpx.Request) -> httpx.Response:
        """按顺序返回不存在、服务失败和无效响应。"""
        return next(responses)

    with Mem0Client("http://mem0.test", transport=httpx.MockTransport(provider)) as client:
        assert client.get("missing") is None
        with pytest.raises(Mem0ApiError, match="503"):
            client.get_all("local-person")
        with pytest.raises(Mem0ApiError, match="无效 JSON"):
            client.get_all("local-person")
