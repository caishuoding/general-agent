"""管理幂等云端操作及真实调用状态。"""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from general_agent.infrastructure.database import Database
from general_agent.infrastructure.provider import TokenUsage
from general_agent.infrastructure.settings import StoredSettings


Unit = Literal["chat_tokens", "embedding_tokens", "credits"]


@dataclass(frozen=True)
class OperationContext:
    """保存一次业务操作的稳定身份、用途和可选会话范围。"""

    operation_id: UUID
    purpose: str
    conversation_id: UUID | None = None


class OperationService:
    """持久化幂等操作、调用生命周期和服务商返回的实际用量。"""

    def __init__(self, database: Database) -> None:
        """绑定共享数据库，不读取价格或执行消费额度判断。"""
        self.database = database

    @contextmanager
    def operation(self, purpose: str, scope: object, operation_id: UUID | None = None, conversation_id: UUID | None = None) -> Iterator[tuple[OperationContext, dict[str, Any] | None]]:
        """锁定稳定操作身份，校验相同标识没有被挪用于其他请求。"""
        identifier = operation_id or uuid4()
        digest = hashlib.sha256(json.dumps([purpose, str(conversation_id), scope], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        context = OperationContext(identifier, purpose, conversation_id)
        with self.database.connect() as connection:
            connection.autocommit = True
            connection.execute("SELECT pg_advisory_lock(%s)", (identifier.int % (2**63 - 1),))
            try:
                connection.execute("INSERT INTO cloud_operations(id,scope_hash,purpose,conversation_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (identifier, digest, purpose, conversation_id))
            except ForeignKeyViolation:
                raise LookupError("会话不存在。") from None
            row = connection.execute("SELECT * FROM cloud_operations WHERE id=%s", (identifier,)).fetchone()
            assert row is not None
            if row["scope_hash"] != digest:
                raise ValueError("操作标识已用于其他内容、会话或配置，请重新发起操作。")
            if row["cancelled"]:
                raise ValueError("该操作已取消，请重新发起操作。")
            yield context, row

    def cache_result(self, context: OperationContext, result: object, status: int) -> None:
        """保存公开响应，重复提交时直接返回而不再次调用外部服务。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE cloud_operations SET result=%s,response_status=%s WHERE id=%s AND cancelled=FALSE", (Jsonb(result), status, context.operation_id))

    def cancel(self, context: OperationContext) -> None:
        """取消尚未开始的后续调用，已发出的调用仍可记录最终状态。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE WHERE id=%s", (context.operation_id,))

    def reserve(self, context: OperationContext, settings: StoredSettings, unit: Unit = "chat_tokens") -> UUID:
        """为一次外部调用创建稳定记录，供取消、恢复和用量核对使用。"""
        call_id = uuid4()
        with self.database.connect() as connection:
            operation = connection.execute("SELECT purpose,conversation_id,cancelled FROM cloud_operations WHERE id=%s FOR UPDATE", (context.operation_id,)).fetchone()
            if not operation or operation["cancelled"]:
                raise ValueError("该操作已取消或上下文无效。")
            if operation["purpose"] != context.purpose or operation["conversation_id"] != context.conversation_id:
                raise ValueError("调用上下文与持久化操作不匹配。")
            connection.execute("INSERT INTO model_calls(id,operation_id,conversation_id,purpose,base_url,model,revision,unit,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'reserved')", (call_id, context.operation_id, context.conversation_id, context.purpose, settings.base_url.rstrip('/'), settings.model, settings.revision, unit))
        return call_id

    def begin(self, context: OperationContext, call_id: UUID, settings: StoredSettings, assistant_message_id: UUID | None = None) -> None:
        """在真正外发前核对取消状态，并保证预留记录只启动一次。"""
        with self.database.connect() as connection:
            row = connection.execute("SELECT cancelled FROM cloud_operations WHERE id=%s FOR UPDATE", (context.operation_id,)).fetchone()
            if not row or row["cancelled"]:
                connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'", (call_id,))
            else:
                started = connection.execute("UPDATE model_calls SET status='generating',assistant_message_id=%s WHERE id=%s AND operation_id=%s AND status='reserved' AND base_url=%s AND model=%s AND revision=%s RETURNING id", (assistant_message_id, call_id, context.operation_id, settings.base_url.rstrip('/'), settings.model, settings.revision)).fetchone()
                if started:
                    return
        raise ValueError("该调用已取消或已经启动，不能重复发送。")

    def record(self, call_id: UUID, usage: TokenUsage, status: str | None = None, credits: Decimal | None = None) -> None:
        """记录服务商返回的 token 或 credits 以及调用终态。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE model_calls SET prompt_tokens=coalesce(%s,prompt_tokens),completion_tokens=coalesce(%s,completion_tokens),total_tokens=coalesce(%s,total_tokens),credits=coalesce(%s,credits),status=CASE WHEN status='stopped' THEN status ELSE coalesce(%s,status) END,finished_at=CASE WHEN %s::text IS NULL THEN finished_at ELSE CURRENT_TIMESTAMP END WHERE id=%s", (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, credits, status, status, call_id))

    def invalidate(self, operation_id: UUID) -> None:
        """取消旧操作并清除缓存结果，防止删除后的内容被重放。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE,invalidated=TRUE,result=NULL,response_status=NULL WHERE id=%s", (operation_id,))

    def has_calls(self, context: OperationContext) -> bool:
        """判断操作是否已经开始过外部调用，避免未知结果被重复发送。"""
        with self.database.connect() as connection:
            return connection.execute("SELECT id FROM model_calls WHERE operation_id=%s AND status <> 'cancelled' LIMIT 1", (context.operation_id,)).fetchone() is not None
