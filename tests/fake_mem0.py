"""为应用测试提供官方 Mem0 REST 形态的确定性内存替身。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from threading import Lock
from typing import Any, Iterable, Mapping
from uuid import uuid4

import httpx


class CallableTransport(httpx.BaseTransport):
    """把同步处理函数稳定地暴露为 httpx 传输，避免复用替身时的事件循环绑定。"""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        """保存只接收 httpx.Request 并返回 Response 的同步处理器。"""
        self.handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """直接调用同步处理器并返回其响应。"""
        return self.handler(request)


class FakeMem0:
    """模拟官方记忆接口，不进入应用业务数据库或真实 Mem0 容器。"""

    def __init__(self) -> None:
        """初始化线程安全的内存记录表。"""
        self.records: dict[str, dict[str, Any]] = {}
        self.lock = Lock()

    def _results(self, payload: Any) -> list[dict[str, Any]]:
        """按官方响应格式展开结果数组。"""
        if isinstance(payload, Mapping) and isinstance(payload.get("results"), list):
            return [dict(item) for item in payload["results"] if isinstance(item, Mapping)]
        return []

    def _fact(self, messages: Iterable[Mapping[str, str]]) -> str:
        """从本轮用户消息提取测试使用的事实，模拟 Mem0 的语义整理边界。"""
        content = ""
        for message in messages:
            if message.get("role") == "user":
                content = str(message.get("content") or "")
        match = re.search(r"(?:请)?记住[：:]?\s*([^；;。]+)", content)
        fact = match.group(1).strip() if match else content.strip()
        return fact.strip(" ：:，,。")

    def _user_records(self, user_id: str) -> list[dict[str, Any]]:
        """返回指定用户的全部记录，保持插入顺序稳定。"""
        return [item for item in self.records.values() if item.get("user_id") == user_id]

    def add(self, messages: Iterable[Mapping[str, str]], user_id: str, metadata: Mapping[str, Any] | None = None, infer: bool = True) -> Any:
        """新增或语义更新一条事实，返回官方 results 形状。"""
        fact = self._fact(messages) if infer else str(list(messages)[-1]["content"])
        with self.lock:
            rows = self._user_records(user_id)
            if "不再喝咖啡" in fact:
                for row in rows:
                    if "咖啡" in row["memory"]:
                        self.records.pop(row["id"])
                        return {"results": [{"id": row["id"], "memory": row["memory"], "event": "DELETE"}]}
            if "苏州" in fact and ("搬到" in fact or "住在" in fact):
                for row in rows:
                    if "杭州" in row["memory"] or "苏州" in row["memory"]:
                        old_memory = row["memory"]
                        row["memory"] = "我住在苏州"
                        row["metadata"] = {**row["metadata"], **dict(metadata or {})}
                        return {"results": [{"id": row["id"], "memory": row["memory"], "old_memory": old_memory, "event": "UPDATE"}]}
            for row in rows:
                if row["memory"] == fact:
                    return {"results": [{"id": row["id"], "memory": fact, "event": "NONE"}]}
            identifier = str(uuid4())
            row = {
                "id": identifier,
                "memory": fact,
                "user_id": user_id,
                "metadata": dict(metadata or {}),
            }
            self.records[identifier] = row
            return {"results": [{"id": identifier, "memory": fact, "event": "ADD"}]}

    def search(self, query: str, user_id: str, limit: int = 50, filters: Mapping[str, Any] | None = None) -> Any:
        """按原文子串返回相关记忆，用于验证白名单与版本核对。"""
        needle = query.strip().lower()
        query_pairs = {needle[index:index + 2] for index in range(max(0, len(needle) - 1))} if needle else set()
        with self.lock:
            candidates = self._user_records(user_id)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in candidates:
            memory = row["memory"].lower()
            memory_pairs = {memory[index:index + 2] for index in range(max(0, len(memory) - 1))}
            overlap = len(query_pairs & memory_pairs)
            city_match = any(token in needle for token in ("城市", "住址", "地址", "居住", "住在")) and any(token in memory for token in ("住在", "杭州", "苏州"))
            if not needle or needle in memory or overlap or city_match:
                score = 1.0 if needle in memory else max(overlap / max(1, len(query_pairs)), 0.9 if city_match else 0.0)
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return {"results": [{**row, "score": score} for score, row in scored[:limit]]}

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """读取单条记忆，不存在时返回空。"""
        with self.lock:
            row = self.records.get(memory_id)
            return dict(row) if row is not None else None

    def get_all(self, user_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """读取用户全部记忆。"""
        with self.lock:
            return [dict(row) for row in self._user_records(user_id)[:limit]]

    def update(self, memory_id: str, data: str, metadata: Mapping[str, Any] | None = None) -> Any:
        """更新正文及元数据，返回官方成功形状。"""
        with self.lock:
            row = self.records.get(memory_id)
            if row is None:
                return {"status": "not-found"}
            row["memory"] = data
            row["metadata"] = {**row["metadata"], **dict(metadata or {})}
            return {"results": [dict(row)]}

    def delete(self, memory_id: str) -> Any:
        """删除指定记忆，重复删除保持幂等。"""
        with self.lock:
            self.records.pop(memory_id, None)
        return {"status": "ok"}

    def close(self) -> None:
        """与官方客户端上下文接口保持一致。"""
        return

    def handle(self, request: httpx.Request) -> httpx.Response:
        """按官方路径分发内存请求，供需要故障注入的 HTTP 传输测试使用。"""
        payload = json.loads(request.content) if request.content else {}
        if request.method == "POST" and request.url.path == "/memories":
            return httpx.Response(200, json=self.add(payload["messages"], payload["user_id"], payload.get("metadata"), payload.get("infer", True)))
        if request.method == "POST" and request.url.path == "/search":
            return httpx.Response(200, json=self.search(payload["query"], payload["user_id"], payload.get("limit", 50), payload.get("filters")))
        if request.method == "GET" and request.url.path == "/memories":
            return httpx.Response(200, json={"results": self.get_all(request.url.params.get("user_id", ""), int(request.url.params.get("page_size", "1000")))})
        if request.method == "GET" and request.url.path.startswith("/memories/"):
            row = self.get(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(404, json={"detail": "not found"}) if row is None else httpx.Response(200, json=row)
        if request.method == "PUT" and request.url.path.startswith("/memories/"):
            return httpx.Response(200, json=self.update(request.url.path.rsplit("/", 1)[-1], payload.get("memory") or payload.get("text", ""), payload.get("metadata")))
        if request.method == "DELETE" and request.url.path.startswith("/memories/"):
            return httpx.Response(200, json=self.delete(request.url.path.rsplit("/", 1)[-1]))
        return httpx.Response(404, json={"detail": "unsupported fake endpoint"})
