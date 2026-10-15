"""通过公开 HTTP、真实 PostgreSQL 与受控服务验证预算和计量。"""

from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app


def test_monthly_threshold_blocks_and_single_approval_is_bound(database_url: str, tmp_path: Path) -> None:
    """已知费用达到预算后暂停，批准仅允许当前操作一次且重复提交不重复执行。"""
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """为真实协议返回固定用量，独立费用样例为输入2元加输出3元。"""
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 1000000, "completion_tokens": 1000000, "total_tokens": 2000000}})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://example.test/v1", "model": "chat", "api_key": "test-only"})
        saved = client.put("/api/settings/budget", json={"monthly_limit": "5", "prices": [{"base_url": "https://example.test/v1", "model": "chat", "input_per_million": "2", "output_per_million": "3"}]})
        assert saved.status_code == 200
        conversation = client.post("/api/conversations", json={}).json()["id"]
        path = f"/api/conversations/{conversation}/messages"
        assert client.post(path, json={"content": "first"}).status_code == 200
        summary = client.get("/api/usage/summary").json()
        assert summary["known_cost"] == "5.000000000000"
        operation = str(uuid4())
        headers = {"X-Operation-ID": operation}
        blocked = client.post(path, json={"content": "second"}, headers=headers)
        assert blocked.status_code == 402
        assert blocked.json()["operation_id"] == operation
        assert blocked.json()["reason"] == "budget_reached"
        assert len(requests) == 1
        assert client.post(f"/api/budget/operations/{operation}/approve", json={"pause_id": blocked.json()["pause_id"]}).status_code == 200
        assert client.post(path, json={"content": "different"}, headers=headers).status_code == 409
        assert client.post(path, json={"content": "second"}, headers=headers).status_code == 200
        assert client.post(path, json={"content": "second"}, headers=headers).status_code == 200
        assert len(requests) == 2
        assert client.post(path, json={"content": "third"}).status_code == 402
        assert len(client.get(path.rsplit("/", 1)[0]).json()["messages"]) == 4


def test_partial_usage_is_preserved_and_unknown_cost_pauses_capabilities(database_url: str, tmp_path: Path) -> None:
    """畸形的一个用量字段不会抹掉其余真实字段，费用未知会阻止下一项能力检查。"""
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """返回有效输入用量及无效输出字段，不能把未知输出当成零。"""
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 17, "completion_tokens": "unknown", "total_tokens": 25}})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://example.test/v1", "model": "chat", "api_key": "test-only"})
        assert client.post("/api/settings/capabilities/text").status_code == 200
        usage = client.get("/api/usage").json()[0]
        assert usage["prompt_tokens"] == 17
        assert usage["completion_tokens"] is None
        assert usage["total_tokens"] == 25
        assert usage["estimated_cost"] is None
        assert client.put("/api/settings/budget", json={"monthly_limit": "10"}).status_code == 200
        paused = client.post("/api/settings/capabilities/image")
        assert paused.status_code == 402
        assert paused.json()["reason"] == "unknown_cost"
        assert len(requests) == 1
        assert client.get("/api/usage/summary").json()["estimated_cost"] is None


def test_prices_resume_embedding_and_never_follow_changed_model(database_url: str, tmp_path: Path) -> None:
    """补齐当前嵌入价格可恢复原暂停操作，模型变更不能沿用不匹配价格。"""
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """返回有效嵌入用量但错误维度，验证失败后用量仍被计价保存。"""
        requests.append(request)
        return httpx.Response(200, json={"model": "embed", "data": [{"index": 0, "embedding": [0.1]}], "usage": {"prompt_tokens": 1000000, "total_tokens": 1000000}})

    transport = httpx.MockTransport(provider)
    with TestClient(create_app(database_url, tmp_path, transport), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings/embedding", json={"base_url": "https://vector.test/v1", "model": "embed", "api_key": "test", "dimensions": 3})
        client.put("/api/settings/budget", json={"monthly_limit": "20"})
        operation = str(uuid4())
        headers = {"X-Operation-ID": operation}
        path = "/api/settings/capabilities/embedding"
        paused = client.post(path, headers=headers)
        assert paused.status_code == 402 and paused.json()["reason"] == "missing_price"
        assert len(requests) == 0
        client.put("/api/settings/budget", json={"monthly_limit": "20", "prices": [{"base_url": "https://vector.test/v1", "model": "embed", "input_per_million": "0.1"}]})
        assert client.get("/api/settings/budget").json()["prices"][0]["input_per_million"] == "0.100000000000"
        resumed = client.post(path, headers=headers)
        assert resumed.json()["status"] == "failed"
        usage = client.get("/api/usage").json()[0]
        assert (usage["estimated_cost"], usage["completion_tokens"]) == ("0.100000000000", None)
        client.put("/api/settings/embedding", json={"base_url": "https://vector.test/v1", "model": "new-model", "dimensions": 3})
        assert client.post(path).json()["reason"] == "missing_price"
        assert client.post(path, headers=headers).status_code == 409
    with TestClient(create_app(database_url, tmp_path, transport), base_url="http://127.0.0.1:8000") as restarted:
        assert restarted.get("/api/usage/summary").json()["known_cost"] == "0.100000000000"
        assert restarted.get("/api/settings/budget").json()["monthly_limit"] == "20.000000000000"


def test_monthly_summary_uses_all_calls_and_backfills_missing_prices(database_url: str, tmp_path: Path) -> None:
    """当月总额覆盖最近100条之外的调用，补价格可恢复此前完整用量的费用。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """每个能力请求返回恰好一百万输入及零输出，以固定样例核对总额。"""
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 1000000, "completion_tokens": 0}})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://example.test/v1", "model": "chat", "api_key": "test"})
        for _ in range(101):
            assert client.post("/api/settings/capabilities/text").status_code == 200
        assert len(client.get("/api/usage").json()) == 100
        summary = client.get("/api/usage/summary").json()
        assert summary["unknown_calls"] == 101 and summary["estimated_cost"] is None
        client.put("/api/settings/budget", json={"monthly_limit": "101", "prices": [{"base_url": "https://example.test/v1", "model": "chat", "input_per_million": "1", "output_per_million": "1"}]})
        summary = client.get("/api/usage/summary").json()
        assert summary["known_cost"] == "101.000000000000"
        assert summary["timezone"] == "Asia/Shanghai" and summary["currency"] == "CNY"
        assert client.post("/api/settings/capabilities/text").json()["reason"] == "budget_reached"


def test_inflight_stop_and_retry_share_budget_without_blocking_stop(database_url: str, tmp_path: Path) -> None:
    """预算启用时并发操作暂停；停止不等云端返回，尾帧计价后重试仍需批准。"""
    from threading import Event
    from time import monotonic, sleep
    from test_streaming import ControlledStream, configure, wait_message

    release = Event()
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """保持一个真实HTTP流等待，供其他公开接口同时操作。"""
        requests.append(request)
        return httpx.Response(200, stream=ControlledStream(release))

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        conversation = configure(client)
        client.put("/api/settings/budget", json={"monthly_limit": "0.000017", "prices": [{"base_url": "https://model.example/v1", "model": "controlled", "input_per_million": "1", "output_per_million": "1"}]})
        try:
            assert client.post(f"/api/conversations/{conversation}/runs", json={"content": "hello"}).status_code == 202
            message = wait_message(client, conversation, "generating", "first ")
            assert client.post("/api/settings/capabilities/text").json()["reason"] == "in_flight"
            assert len(requests) == 1
            stopped = client.post(f"/api/conversations/{conversation}/messages/{message['id']}/stop")
            assert stopped.status_code == 200
            assert client.get("/api/usage/summary").json()["estimated_cost"] is None
        finally:
            release.set()
        deadline = monotonic() + 5
        while monotonic() < deadline and client.get("/api/usage/summary").json()["pending_calls"]:
            sleep(0.02)
        assert client.get("/api/usage/summary").json()["known_cost"] == "0.000017000000"
        assert client.get("/api/usage").json()[0]["status"] == "stopped"
        retry_path = f"/api/conversations/{conversation}/messages/{message['id']}/retry"
        blocked = client.post(retry_path)
        assert blocked.status_code == 402 and blocked.json()["reason"] == "budget_reached"
        operation = blocked.json()["operation_id"]
        assert client.post(f"/api/budget/operations/{operation}/approve", json={"pause_id": blocked.json()["pause_id"]}).status_code == 200
        assert client.post(retry_path, headers={"X-Operation-ID": operation}).status_code == 202
        wait_message(client, conversation, "complete")
        assert len(client.get("/api/usage").json()) == 2
        assert client.post(f"/api/budget/operations/{operation}/approve", json={"pause_id": blocked.json()["pause_id"]}).status_code == 409


def test_cancelled_pause_cannot_be_approved_or_replayed(database_url: str, tmp_path: Path) -> None:
    """取消撤回当前操作及暂停挑战，旧操作标识不能再执行或转移到其他会话。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """被取消操作不应到达云端，一旦到达立即使测试失败。"""
        raise AssertionError("取消后不应发送外部请求")

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://example.test/v1", "model": "chat", "api_key": "test"})
        client.put("/api/settings/budget", json={"monthly_limit": "0"})
        conversation = client.post("/api/conversations", json={}).json()["id"]
        path = f"/api/conversations/{conversation}/runs"
        paused = client.post(path, json={"content": "private-content"}).json()
        operation = paused["operation_id"]
        assert "private-content" not in str(paused)
        assert client.post(f"/api/budget/operations/{operation}/cancel").status_code == 200
        assert client.post(f"/api/budget/operations/{operation}/approve", json={"pause_id": paused["pause_id"]}).status_code == 409
        assert client.post(path, json={"content": "private-content"}, headers={"X-Operation-ID": operation}).status_code == 409
        assert client.get(f"/api/conversations/{conversation}").json()["messages"] == []
        assert client.get("/api/usage").json() == []


def test_monthly_window_starts_at_shanghai_midnight(database_url: str, tmp_path: Path) -> None:
    """以数据库时间前置夹具覆盖上海月初两侧，最终通过公开汇总与预算判断验收。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    import psycopg

    def provider(request: httpx.Request) -> httpx.Response:
        """提供每次一元的固定用量用于独立核对月份边界。"""
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 1000000, "completion_tokens": 0}})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://example.test/v1", "model": "chat", "api_key": "test"})
        client.put("/api/settings/budget", json={"prices": [{"base_url": "https://example.test/v1", "model": "chat", "input_per_million": "1", "output_per_million": "1"}]})
        for _ in range(2):
            assert client.post("/api/settings/capabilities/text").status_code == 200
        calls = client.get("/api/usage").json()
        start = datetime.now(ZoneInfo("Asia/Shanghai")).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with psycopg.connect(database_url) as connection:
            connection.execute("UPDATE model_calls SET created_at=%s WHERE id=%s", (start - timedelta(microseconds=1), calls[0]["id"]))
            connection.execute("UPDATE model_calls SET created_at=%s WHERE id=%s", (start, calls[1]["id"]))
        summary = client.get("/api/usage/summary").json()
        assert summary["calls"] == 1
        assert summary["known_cost"] == "1.000000000000"
        assert summary["month"] == start.strftime("%Y-%m")


def test_missing_conversation_is_rejected_before_operation_creation(database_url: str, tmp_path: Path) -> None:
    """加入操作外键后，不存在的会话仍应返回404，不能出现内部数据库异常。"""
    with TestClient(create_app(database_url, tmp_path), base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings", json={"base_url": "https://example.test/v1", "model": "chat", "api_key": "test"})
        missing = uuid4()
        for suffix in ("messages", "runs"):
            assert client.post(f"/api/conversations/{missing}/{suffix}", json={"content": "hello"}).status_code == 404
        assert client.post(f"/api/conversations/{missing}/messages/{uuid4()}/retry").status_code == 404
