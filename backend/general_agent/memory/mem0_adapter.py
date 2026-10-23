"""官方 Mem0 自托管 REST API 客户端。

项目 PostgreSQL 只保存业务状态；Mem0 的模型、向量、历史和记忆正文均由
独立 Docker 服务管理，本模块不再导入本地 SDK，也不直接访问 Mem0 数据库。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

import httpx


class Mem0ApiError(ValueError):
    """表示 Mem0 服务返回的可诊断错误。"""


class Mem0Client:
    """封装官方 Mem0 REST API，并统一处理认证、超时和响应格式。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 60.0, transport: httpx.BaseTransport | None = None) -> None:
        """创建客户端；地址和密钥默认读取环境变量，测试可注入受控传输。"""
        self.base_url = (base_url or os.getenv("MEM0_API_URL") or "http://127.0.0.1:8888").rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("MEM0_API_KEY", "")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        self._client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout, transport=transport)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """发送请求并将非成功响应转换成统一的业务异常。"""
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise Mem0ApiError(f"Mem0 服务不可用：{error}") from error
        if response.is_error:
            detail = response.text[:1000]
            raise Mem0ApiError(f"Mem0 API {response.status_code}：{detail}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise Mem0ApiError("Mem0 API 返回了无效 JSON。") from error

    @staticmethod
    def _results(payload: Any) -> list[dict[str, Any]]:
        """兼容官方接口返回的 results 数组和单条记忆对象。"""
        if isinstance(payload, Mapping):
            result = payload.get("results")
            if isinstance(result, list):
                return [dict(item) for item in result if isinstance(item, Mapping)]
            if payload.get("id"):
                return [dict(payload)]
        return []

    def add(self, messages: Iterable[Mapping[str, str]], user_id: str, metadata: Mapping[str, Any] | None = None, infer: bool = True) -> Any:
        """让 Mem0 服务提取或保存记忆，返回官方 add 响应。"""
        body: dict[str, Any] = {"messages": [dict(item) for item in messages], "user_id": user_id, "infer": infer}
        if metadata:
            body["metadata"] = dict(metadata)
        return self._request("POST", "/memories", json=body)

    def search(self, query: str, user_id: str, limit: int = 50, filters: Mapping[str, Any] | None = None) -> Any:
        """调用 Mem0 长期记忆搜索并返回官方响应；混合排序由 Mem0 内部实现。"""
        body: dict[str, Any] = {"query": query, "user_id": user_id, "limit": limit}
        if filters:
            body["filters"] = dict(filters)
        return self._request("POST", "/search", json=body)

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """按 Mem0 正式 ID 获取单条记忆，不存在时返回 None。"""
        try:
            payload = self._request("GET", f"/memories/{memory_id}")
        except Mem0ApiError as error:
            if "404" in str(error):
                return None
            raise
        return dict(payload) if isinstance(payload, Mapping) else None

    def get_all(self, user_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """读取用户的全部记忆，统一展开官方返回格式。"""
        return self._results(self._request("GET", "/memories", params={"user_id": user_id, "page_size": limit}))

    def update(self, memory_id: str, data: str, metadata: Mapping[str, Any] | None = None) -> Any:
        """更新指定 Mem0 记忆的正文及可选元数据。"""
        body: dict[str, Any] = {"text": data, "memory": data}
        if metadata:
            body["metadata"] = dict(metadata)
        return self._request("PUT", f"/memories/{memory_id}", json=body)

    def delete(self, memory_id: str) -> Any:
        """删除指定 Mem0 记忆。"""
        return self._request("DELETE", f"/memories/{memory_id}")

    def close(self) -> None:
        """释放 HTTP 连接池。"""
        self._client.close()

    def __enter__(self) -> "Mem0Client":
        """进入上下文并返回客户端。"""
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """退出上下文并释放连接。"""
        self.close()

