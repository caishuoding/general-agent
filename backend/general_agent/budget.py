"""统一的持久化操作、月度预算检查和实际调用计量，跨线程显式传递上下文。"""

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
from pydantic import BaseModel, Field

from general_agent.database import Database
from general_agent.provider import TokenUsage
from general_agent.settings import StoredSettings

Money = Decimal
Unit = Literal["chat_tokens", "embedding_tokens", "credits"]
LOCK = 91402704
MONTH = "date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'"
REASONS = {
    "budget_reached": "本月估算费用已达到月度预算。",
    "missing_price": "当前服务或模型缺少单价，费用未知。请补齐单价或允许本次继续。",
    "unknown_cost": "本月存在费用未知的调用。请补齐单价，或允许本次继续。",
    "in_flight": "已有请求正在等待实际用量，当前总费用未知。可等待完成后重新检查。",
}


class PriceInput(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    input_per_million: Money | None = Field(default=None, ge=0, max_digits=24, decimal_places=12)
    output_per_million: Money | None = Field(default=None, ge=0, max_digits=24, decimal_places=12)
    per_credit: Money | None = Field(default=None, ge=0, max_digits=24, decimal_places=12)


class BudgetInput(BaseModel):
    monthly_limit: Money | None = Field(default=None, ge=0, max_digits=24, decimal_places=12)
    prices: list[PriceInput] = Field(default_factory=list, max_length=200)


@dataclass(frozen=True)
class OperationContext:
    """与服务端操作绑定的不可变上下文，可安全传入 SDK 内部工作线程。"""
    operation_id: UUID
    purpose: str
    conversation_id: UUID | None = None


class BudgetPaused(Exception):
    """携带持久化暂停挑战，客户端只能批准这个操作的下一次调用。"""

    def __init__(self, operation_id: UUID, reason: str, pause_id: UUID) -> None:
        """保存无请求正文的公开暂停信息和批准所需挑战标识。"""
        super().__init__(REASONS[reason])
        self.operation_id, self.reason, self.pause_id = operation_id, reason, pause_id

    def public(self) -> dict[str, str]:
        """返回界面恢复操作所需的固定信息，避免暴露云端错误正文。"""
        return {"detail": str(self), "code": "budget_paused", "operation_id": str(self.operation_id), "reason": self.reason, "pause_id": str(self.pause_id), "approval_scope": "next_call"}


class BudgetService:
    """在 PostgreSQL 中串行决定每次调用的准入，计量独立提交以保留失败用量。"""

    def __init__(self, database: Database) -> None:
        """绑定共享数据库；不读取进程隐式上下文或任何服务凭据。"""
        self.database = database

    def settings(self) -> dict[str, Any]:
        """读取以人民币计价的预算及服务模型对应单价。"""
        with self.database.connect() as connection:
            row = connection.execute("SELECT monthly_limit FROM budget_settings").fetchone()
            assert row is not None
            return {"monthly_limit": format(row["monthly_limit"], "f") if row["monthly_limit"] is not None else None, "currency": "CNY", "timezone": "Asia/Shanghai", "prices": [{key: format(value, "f") if isinstance(value, Decimal) else value for key, value in price.items()} for price in connection.execute("SELECT * FROM model_prices ORDER BY base_url, model").fetchall()]}

    def save(self, body: BudgetInput) -> dict[str, Any]:
        """原子更新预算及单价，只为之前无法估算且用量完整的记录补算费用。"""
        with self.database.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
            connection.execute("UPDATE budget_settings SET monthly_limit = %s", (body.monthly_limit,))
            connection.execute("DELETE FROM model_prices")
            for price in body.prices:
                connection.execute("INSERT INTO model_prices VALUES (%s,%s,%s,%s,%s) ON CONFLICT(base_url,model) DO UPDATE SET input_per_million=EXCLUDED.input_per_million,output_per_million=EXCLUDED.output_per_million,per_credit=EXCLUDED.per_credit", (price.base_url.rstrip('/'), price.model, price.input_per_million, price.output_per_million, price.per_credit))
            connection.execute("UPDATE model_calls c SET input_price=p.input_per_million, output_price=p.output_per_million, credit_price=p.per_credit FROM model_prices p WHERE c.base_url=p.base_url AND c.model=p.model AND c.estimated_cost IS NULL")
            connection.execute("UPDATE model_calls SET estimated_cost=" + self.cost_sql() + " WHERE estimated_cost IS NULL AND status NOT IN ('reserved','cancelled')")
        return self.settings()

    @staticmethod
    def cost_sql() -> str:
        """以十进制定点单价计算费用，缺少所需字段时 SQL 保持 NULL。"""
        return "CASE unit WHEN 'credits' THEN credits*credit_price WHEN 'embedding_tokens' THEN prompt_tokens*input_price/1000000 ELSE (prompt_tokens*input_price+completion_tokens*output_price)/1000000 END"

    def summary(self) -> dict[str, Any]:
        """聚合上海时区当前自然月全部调用，近期列表上限不影响预算。"""
        with self.database.connect() as connection:
            row = connection.execute("SELECT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai','YYYY-MM') AS month, count(*) AS calls, coalesce(sum(estimated_cost),0)::numeric(38,12) AS known_cost, count(*) FILTER(WHERE estimated_cost IS NULL) AS unknown_calls, count(*) FILTER(WHERE finished_at IS NULL) AS pending_calls, sum(prompt_tokens) AS prompt_tokens, sum(completion_tokens) AS completion_tokens, sum(credits) AS credits FROM model_calls WHERE created_at >= " + MONTH + " AND status <> 'cancelled'").fetchone()
            assert row is not None
            row["known_cost"] = format(row["known_cost"], "f")
            row["estimated_cost"] = row["known_cost"] if row["unknown_calls"] == 0 else None
            return {**row, "currency": "CNY", "timezone": "Asia/Shanghai", "monthly_limit": self.settings()["monthly_limit"]}

    @contextmanager
    def operation(self, purpose: str, scope: object, operation_id: UUID | None = None, conversation_id: UUID | None = None) -> Iterator[tuple[OperationContext, dict[str, Any] | None]]:
        """串行处理同一操作ID，校验会话和不可挪用范围，返回此前已完成的HTTP结果。"""
        identifier = operation_id or uuid4()
        digest = hashlib.sha256(json.dumps([purpose, str(conversation_id), scope], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        context = OperationContext(identifier, purpose, conversation_id)
        with self.database.connect() as connection:
            connection.autocommit = True
            connection.execute("SELECT pg_advisory_lock(%s)", (identifier.int % (2**63 - 1),))
            try:
                connection.execute("INSERT INTO cloud_operations(id,scope_hash,purpose,conversation_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (identifier,digest,purpose,conversation_id))
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
        """持久化操作的公开HTTP结果，使重复提交不会再次发送云端请求。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE cloud_operations SET result=%s,response_status=%s WHERE id=%s AND cancelled=FALSE", (Jsonb(result),status,context.operation_id))

    def approve(self, operation_id: UUID, pause_id: UUID) -> None:
        """仅批准仍有效的暂停挑战一次，重复或过期批准不能用于后续调用。"""
        with self.database.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
            row = connection.execute("SELECT * FROM cloud_operations WHERE id=%s FOR UPDATE", (operation_id,)).fetchone()
            if not row or row["cancelled"] or row["pause_id"] != pause_id or row["result"] is not None:
                raise ValueError("暂停请求已失效，请重新发起操作。")
            connection.execute("UPDATE cloud_operations SET approved=TRUE WHERE id=%s", (operation_id,))

    def cancel(self, context: OperationContext) -> None:
        """取消尚未发出的后续调用；已发出调用仍允许保存尾帧用量。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE,approved=FALSE WHERE id=%s", (context.operation_id,))

    def reserve(self, context: OperationContext, settings: StoredSettings, unit: Unit = "chat_tokens") -> UUID:
        """在短事务中检查预算、消耗一次批准并保留调用位置，防止并发穿透暂停。"""
        paused: BudgetPaused | None = None
        call_id = uuid4()
        with self.database.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
            operation = connection.execute("SELECT * FROM cloud_operations WHERE id=%s FOR UPDATE", (context.operation_id,)).fetchone()
            if not operation or operation["cancelled"]:
                raise ValueError("该操作已取消或上下文无效。")
            if operation["purpose"] != context.purpose or operation["conversation_id"] != context.conversation_id:
                raise ValueError("调用上下文与持久化操作不匹配。")
            price = connection.execute("SELECT * FROM model_prices WHERE base_url=%s AND model=%s", (settings.base_url.rstrip('/'),settings.model)).fetchone() or {}
            budget = connection.execute("SELECT monthly_limit FROM budget_settings").fetchone()
            assert budget is not None
            reason = None
            if budget["monthly_limit"] is not None and not operation["approved"]:
                totals = connection.execute("SELECT coalesce(sum(estimated_cost),0) AS cost, count(*) FILTER(WHERE estimated_cost IS NULL) AS unknown, count(*) FILTER(WHERE finished_at IS NULL) AS pending FROM model_calls WHERE created_at >= " + MONTH + " AND status <> 'cancelled'").fetchone()
                assert totals is not None
                required = ["per_credit"] if unit == "credits" else ["input_per_million"] if unit == "embedding_tokens" else ["input_per_million","output_per_million"]
                if totals["pending"]:
                    reason = "in_flight"
                elif totals["unknown"]:
                    reason = "unknown_cost"
                elif totals["cost"] >= budget["monthly_limit"]:
                    reason = "budget_reached"
                elif any(price.get(key) is None for key in required):
                    reason = "missing_price"
            if reason:
                pause_id = operation["pause_id"] or uuid4()
                connection.execute("UPDATE cloud_operations SET pause_id=%s,reason=%s WHERE id=%s", (pause_id,reason,context.operation_id))
                paused = BudgetPaused(context.operation_id,reason,pause_id)
            else:
                connection.execute("UPDATE cloud_operations SET approved=FALSE,pause_id=NULL,reason=NULL WHERE id=%s", (context.operation_id,))
                connection.execute("INSERT INTO model_calls(id,operation_id,conversation_id,purpose,base_url,model,revision,unit,status,input_price,output_price,credit_price) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'reserved',%s,%s,%s)", (call_id,context.operation_id,context.conversation_id,context.purpose,settings.base_url.rstrip('/'),settings.model,settings.revision,unit,price.get("input_per_million"),price.get("output_per_million"),price.get("per_credit")))
        if paused:
            raise paused
        return call_id

    def begin(self, context: OperationContext, call_id: UUID, settings: StoredSettings, assistant_message_id: UUID | None = None) -> None:
        """实际发送前再次核对取消状态，预留调用只能启动一次。"""
        with self.database.connect() as connection:
            row = connection.execute("SELECT cancelled FROM cloud_operations WHERE id=%s FOR UPDATE", (context.operation_id,)).fetchone()
            if not row or row["cancelled"]:
                connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'", (call_id,))
            else:
                started = connection.execute("UPDATE model_calls SET status='generating',assistant_message_id=%s WHERE id=%s AND operation_id=%s AND status='reserved' AND base_url=%s AND model=%s AND revision=%s RETURNING id", (assistant_message_id,call_id,context.operation_id,settings.base_url.rstrip('/'),settings.model,settings.revision)).fetchone()
                if started:
                    return
        raise ValueError("该调用已取消或已经启动，不能重复发送。")

    def record(self, call_id: UUID, usage: TokenUsage, status: str | None = None, credits: Decimal | None = None) -> None:
        """先提交可取得的用量，累计尾帧覆盖同一调用而不重复记账。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE model_calls SET prompt_tokens=coalesce(%s,prompt_tokens),completion_tokens=coalesce(%s,completion_tokens),total_tokens=coalesce(%s,total_tokens),credits=coalesce(%s,credits),status=CASE WHEN status='stopped' THEN status ELSE coalesce(%s,status) END,finished_at=CASE WHEN %s::text IS NULL THEN finished_at ELSE CURRENT_TIMESTAMP END WHERE id=%s", (usage.prompt_tokens,usage.completion_tokens,usage.total_tokens,credits,status,status,call_id))
            connection.execute("UPDATE model_calls SET estimated_cost=" + self.cost_sql() + " WHERE id=%s", (call_id,))

    def invalidate(self, operation_id: UUID) -> None:
        """失效已删除来源关联操作的结果与批准，防止幂等重放旧正文或过时成功状态。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE,approved=FALSE,result=NULL,response_status=NULL WHERE id=%s", (operation_id,))

    def has_calls(self, context: OperationContext) -> bool:
        """识别结果尚未缓存但已开始的执行，HTTP重放不得盲目再次发送云端请求。"""
        with self.database.connect() as connection:
            return connection.execute("SELECT id FROM model_calls WHERE operation_id=%s AND status <> 'cancelled' LIMIT 1", (context.operation_id,)).fetchone() is not None

    def invalidate_conversation(self, conversation_id: UUID) -> None:
        """清除会话来源关联的所有缓存正文并撤销许可，供删除来源流程调用。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE,approved=FALSE,result=NULL,response_status=NULL WHERE conversation_id=%s", (conversation_id,))
