"""提供长期记忆更正的 HTTP 适配器；页面删除后仍保留接口供测试和外部工具使用。"""

from uuid import UUID, uuid5

from fastapi import APIRouter, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from general_agent.memory.correction import MemoryCorrection


class CorrectionInput(BaseModel):
    """描述一次带版本保护的记忆更正请求。"""

    fact: str = Field(min_length=1, max_length=5000)
    expected_version: int = Field(ge=1)

    @field_validator("fact")
    @classmethod
    def clean_fact(cls, value: str) -> str:
        """清理更正正文并拒绝空白输入。"""
        if not value.strip():
            raise ValueError("请填写具体事实。")
        return value.strip()


def correction_router(correction: MemoryCorrection) -> APIRouter:
    """绑定长期记忆更正服务，保留稳定操作和失败重试接口。"""
    router = APIRouter()
    operations = correction.memory.calls.operations

    def perform(action_id: UUID, body: CorrectionInput, operation_id: UUID | None, update_id: UUID | None = None) -> JSONResponse:
        """执行带版本和幂等保护的更正操作。"""
        scope = [str(action_id), body.fact, body.expected_version, str(update_id) if update_id else None]
        try:
            with operations.operation("memory_correction", scope, operation_id) as (operation, previous):
                if previous and previous["result"] is not None:
                    return JSONResponse(previous["result"], status_code=previous["response_status"])
                identifier = update_id or uuid5(operation.operation_id, "memory-correction")
                try:
                    result = correction.apply(action_id, body.fact, body.expected_version, identifier, operation)
                except ValueError as error:
                    failure = {"detail": str(error)}
                    operations.cache_result(operation, failure, 409)
                    return JSONResponse(failure, status_code=409)
                public = jsonable_encoder(result)
                operations.cache_result(operation, public, 200)
                return JSONResponse(public)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @router.put("/api/memories/{action_id}")
    def edit_memory(action_id: UUID, body: CorrectionInput, operation_id: UUID | None = Header(default=None, alias="X-Operation-ID")) -> JSONResponse:
        """通过记忆标识提交一次明确更正。"""
        return perform(action_id, body, operation_id)

    @router.post("/api/memory-updates/{identifier}/retry")
    def retry_memory(identifier: UUID, operation_id: UUID | None = Header(default=None, alias="X-Operation-ID")) -> JSONResponse:
        """重试未完成的更正，并复用原目标版本。"""
        try:
            row = correction.get(identifier)
            if row["status"] == "deleted":
                raise HTTPException(409, "该记忆已删除，旧更正批准已失效。")
            return perform(row["action_id"], CorrectionInput(fact=row["fact"], expected_version=row["target_version"] - 1), operation_id, identifier)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    return router
