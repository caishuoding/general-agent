"""验证供 SDK 使用的公开网关契约：显式跨线程上下文与逐调用批准。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app
from general_agent.budget import BudgetPaused, BudgetService
from general_agent.calls import CallGateway
from general_agent.settings import StoredEmbeddingSettings


def test_explicit_thread_context_requires_approval_for_each_cloud_call(database_url: str, tmp_path: Path) -> None:
    """绑定适配器在线程池里仍计量同一操作，一次批准只允许其中下一次HTTP。"""
    outbound: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """为真实嵌入协议返回可校验向量和输入用量。"""
        outbound.append(request)
        return httpx.Response(200, json={"model": "embed", "data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 4, "total_tokens": 4}})

    app = create_app(database_url, tmp_path, httpx.MockTransport(provider))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.put("/api/settings/budget", json={"monthly_limit": "0"})
        budget: BudgetService = app.state.budget
        gateway: CallGateway = app.state.calls
        saved = StoredEmbeddingSettings(base_url="https://vectors.test/v1", model="embed", api_key="test", dimensions=2)
        operation_id = uuid4()
        with budget.operation("memory_save", "source-identity", operation_id) as (context, _), ThreadPoolExecutor(max_workers=1) as executor:
            bound = gateway.bind(context)
            with pytest.raises(BudgetPaused) as blocked:
                executor.submit(bound.embed, saved, ["已确认事实"]).result()
            first_pause = blocked.value.public()
            assert len(outbound) == 0
            assert client.post(f"/api/budget/operations/{operation_id}/approve", json={"pause_id": first_pause["pause_id"]}).status_code == 200
            result = executor.submit(bound.embed, saved, ["已确认事实"]).result()
            assert result.dimensions == 2
            with pytest.raises(BudgetPaused) as next_blocked:
                executor.submit(bound.embed, saved, ["下一项"]).result()
            assert next_blocked.value.pause_id != UUID(first_pause["pause_id"])
            assert client.post(f"/api/budget/operations/{operation_id}/approve", json={"pause_id": first_pause["pause_id"]}).status_code == 409
            assert len(outbound) == 1
            usage = client.get("/api/usage").json()[0]
            assert usage["operation_id"] == str(operation_id)
            assert usage["purpose"] == "memory_save" and usage["prompt_tokens"] == 4
            budget.cancel(context)
            with pytest.raises(ValueError, match="取消"):
                executor.submit(bound.embed, saved, ["取消后的项"]).result()
            assert len(outbound) == 1
