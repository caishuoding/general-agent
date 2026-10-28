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
from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.infrastructure.provider import TokenUsage
from general_agent.infrastructure.settings import StoredSettings


Unit = Literal["chat_tokens", "embedding_tokens", "credits"]
WebKind = Literal["search", "fetch"]


class CallBudgetExceeded(ValueError):
    """表示根回答的共享请求预算已耗尽，后续相同类型请求不得继续外发。"""


@dataclass(frozen=True)
class OperationContext:
    """保存一次业务操作的稳定身份、用途和可选会话范围。"""

    operation_id: UUID
    purpose: str
    conversation_id: UUID | None = None
    account_id: UUID | None = None


class OperationService:
    """持久化幂等操作、调用生命周期和服务商返回的实际用量。"""

    def __init__(self, database: Database) -> None:
        """绑定共享数据库，不读取价格或执行消费额度判断。"""
        self.database = database

    @contextmanager
    def operation(self, purpose: str, scope: object, operation_id: UUID | None = None, conversation_id: UUID | None = None, account_id: UUID | None = None) -> Iterator[tuple[OperationContext, dict[str, Any] | None]]:
        """锁定稳定操作身份，校验相同标识没有被挪用于其他请求。"""
        identifier = operation_id or uuid4()
        digest = hashlib.sha256(json.dumps([purpose, str(conversation_id), str(account_id), scope], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        context = OperationContext(identifier, purpose, conversation_id, account_id)
        with self.database.connect() as connection:
            connection.autocommit = True
            connection.execute("SELECT pg_advisory_lock(%s)", (identifier.int % (2**63 - 1),))
            try:
                connection.execute("INSERT INTO cloud_operations(id,scope_hash,purpose,conversation_id,user_id) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (identifier, digest, purpose, conversation_id, account_id))
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
            operation = connection.execute("SELECT purpose,conversation_id,user_id,cancelled FROM cloud_operations WHERE id=%s FOR UPDATE", (context.operation_id,)).fetchone()
            if not operation or operation["cancelled"]:
                raise ValueError("该操作已取消或上下文无效。")
            if operation["purpose"] != context.purpose or operation["conversation_id"] != context.conversation_id or operation.get("user_id") != context.account_id:
                raise ValueError("调用上下文与持久化操作不匹配。")
            connection.execute("INSERT INTO model_calls(id,operation_id,conversation_id,user_id,purpose,base_url,model,revision,unit,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved')", (call_id, context.operation_id, context.conversation_id, context.account_id, context.purpose, settings.base_url.rstrip('/'), settings.model, settings.revision, unit))
        return call_id

    def begin(self, context: OperationContext, call_id: UUID, settings: StoredSettings, assistant_message_id: UUID | None = None,
              web_kind: WebKind | None = None) -> None:
        """输入操作、预留、配置及联网类别，外发前核验期限与分类额度；成功后无返回值。"""
        with self.database.connect() as connection:
            run = connection.execute('SELECT r.id,r.agent_id,r.deadline,r.status,a.answer_attempt_id,a.parent_agent_id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE r.operation_id=%s', (context.operation_id,)).fetchone()
            if run:
                root = connection.execute("SELECT a.* FROM agents a JOIN messages m ON m.id=a.answer_attempt_id JOIN conversations c ON c.id=a.conversation_id WHERE a.answer_attempt_id=%s AND a.parent_agent_id IS NULL AND m.status='generating' AND NOT c.deleting FOR UPDATE OF a", (run['answer_attempt_id'],)).fetchone()
                latest = connection.execute('SELECT status,deadline FROM agent_runs WHERE id=%s', (run['id'],)).fetchone()
                if latest:
                    run.update(latest)
                from datetime import datetime, timezone
                if not root or root['closed'] or run['status'] not in ('queued','running') or min(root['deadline'],run['deadline']) <= datetime.now(timezone.utc):
                    raise ValueError('回答或智能体执行已停止或超时。')
                unit_row = connection.execute('SELECT unit FROM model_calls WHERE id=%s', (call_id,)).fetchone()
                assert unit_row is not None
                if unit_row['unit'] == 'credits':
                    if web_kind not in ('search', 'fetch'):
                        raise ValueError('联网请求缺少搜索或抓取类别。')
                    # 搜索与抓取共享同一智能体额度，避免两类请求各自接近上限造成总成本失控。
                    field = 'web_count'
                    limit = LIMITS.web_calls
                    agent = (root if run['agent_id'] == root['id'] else
                             connection.execute(f'SELECT {field} FROM agents WHERE id=%s FOR UPDATE', (run['agent_id'],)).fetchone())
                    assert agent is not None
                    if agent[field] >= limit:
                        raise CallBudgetExceeded(f'当前智能体已达到{limit}次联网工具请求上限。')
                    connection.execute(f'UPDATE agents SET {field}={field}+1 WHERE id=%s', (run['agent_id'],))
                else:
                    if root['model_count'] >= LIMITS.model_calls:
                        raise CallBudgetExceeded('本次回答已达到共享调用预算。')
                    connection.execute('UPDATE agents SET model_count=model_count+1 WHERE id=%s', (root['id'],))
                connection.execute('UPDATE model_calls SET agent_id=%s,agent_run_id=%s,assistant_message_id=%s WHERE id=%s', (run['agent_id'],run['id'],run['answer_attempt_id'],call_id))
                assistant_message_id = run['answer_attempt_id']
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

    def cancel_reservation(self, call_id: UUID) -> None:
        """仅终结未外发的预留，保留已开始或已完成调用的真实状态及用量。"""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP,"
                "credits=CASE WHEN unit='credits' THEN 0 ELSE credits END WHERE id=%s AND status='reserved'",
                (call_id,),
            )

    def invalidate(self, operation_id: UUID) -> None:
        """取消旧操作并清除缓存结果，防止删除后的内容被重放。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE,invalidated=TRUE,result=NULL,response_status=NULL WHERE id=%s", (operation_id,))
