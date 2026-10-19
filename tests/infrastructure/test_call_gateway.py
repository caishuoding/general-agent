"""验证供 SDK 使用的公开网关契约：显式跨线程上下文、用量记录与取消。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.operations import OperationService
from general_agent.infrastructure.settings import StoredEmbeddingSettings
from tests.auth.support import login_admin


def test_explicit_thread_context_records_each_cloud_call_and_honours_cancellation(database_url: str, tmp_path: Path) -> None:
    """绑定适配器在线程池里仍计量同一操作，取消后不再发送新的 HTTP 请求。"""
    outbound: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """为真实嵌入协议返回可校验向量和输入用量。"""
        outbound.append(request)
        return httpx.Response(200, json={"model": "embed", "data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 4, "total_tokens": 4}})

    app = create_app(database_url, tmp_path, httpx.MockTransport(provider))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        login_admin(client)
        account_id = UUID(client.get("/api/auth/me").json()["id"])
        operations: OperationService = app.state.operations
        gateway: CallGateway = app.state.calls
        saved = StoredEmbeddingSettings(base_url="https://vectors.test/v1", model="embed", api_key="test", dimensions=2)
        operation_id = uuid4()
        with operations.operation("memory_save", "source-identity", operation_id, account_id=account_id) as (context, _), ThreadPoolExecutor(max_workers=1) as executor:
            bound = gateway.bind(context)
            result = executor.submit(bound.embed, saved, ["已确认事实"]).result()
            assert result.dimensions == 2
            executor.submit(bound.embed, saved, ["下一项"]).result()
            assert len(outbound) == 2
            usages = client.get("/api/usage").json()
            assert len(usages) == 2
            assert all(item["operation_id"] == str(operation_id) for item in usages)
            assert all(item["purpose"] == "memory_save" and item["prompt_tokens"] == 4 for item in usages)
            operations.cancel(context)
            try:
                executor.submit(bound.embed, saved, ["取消后的项"]).result()
            except ValueError as error:
                assert "取消" in str(error)
            else:
                raise AssertionError("取消后的操作不应继续发送嵌入请求。")
            assert len(outbound) == 2
