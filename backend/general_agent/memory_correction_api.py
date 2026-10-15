"""管理页明确编辑入口，操作批准绑定条目、版本、正文和嵌入配置。"""

from typing import Any
from uuid import UUID, uuid5

from fastapi import APIRouter, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from general_agent.budget import BudgetPaused
from general_agent.memory_correction import MemoryCorrection


class CorrectionInput(BaseModel):
    fact: str = Field(min_length=1,max_length=5000)
    expected_version: int = Field(ge=1)

    @field_validator('fact')
    @classmethod
    def clean_fact(cls, value: str) -> str:
        """禁止空白更正正文，保留用户批准的具体事实文本。"""
        if not value.strip():
            raise ValueError('请填写具体事实。')
        return value.strip()


def correction_router(correction: MemoryCorrection) -> APIRouter:
    """绑定独立更正服务，并提供同一批准目标的显式失败重试。"""
    router=APIRouter()
    budget=correction.memory.calls.budget

    def perform(action_id: UUID, body: CorrectionInput, operation_id: UUID | None, update_id: UUID | None = None) -> JSONResponse:
        """按持久化操作防重；暂停不缓存失败，实际失败需新操作显式重试。"""
        settings=correction.memory.embedding.store.read()
        scope=[str(action_id),body.fact,body.expected_version,str(update_id) if update_id else None,settings.revision if settings else None]
        try:
            with budget.operation('memory_correction',scope,operation_id) as (operation,previous):
                if previous and previous['result'] is not None:
                    return JSONResponse(previous['result'],status_code=previous['response_status'])
                identifier=update_id or uuid5(operation.operation_id,'memory-correction')
                try:
                    result=correction.apply(action_id,body.fact,body.expected_version,identifier,operation)
                except ValueError as error:
                    failure={'detail':str(error)}
                    budget.cache_result(operation,failure,409)
                    return JSONResponse(failure,status_code=409)
                public=jsonable_encoder(result)
                budget.cache_result(operation,public,200)
                return JSONResponse(public)
        except BudgetPaused as paused:
            return JSONResponse(paused.public(),status_code=402)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @router.put('/api/memories/{action_id}')
    def edit_memory(action_id: UUID, body: CorrectionInput, operation_id: UUID | None = Header(default=None,alias='X-Operation-ID')) -> JSONResponse:
        """直接应用用户在管理页批准的明确条目修改，无需重复确认正文。"""
        return perform(action_id,body,operation_id)

    @router.post('/api/memory-updates/{identifier}/retry')
    def retry_memory(identifier: UUID, operation_id: UUID | None = Header(default=None,alias='X-Operation-ID')) -> JSONResponse:
        """复用原更正目标和来源，新尝试先核对部分提交再决定是否嵌入。"""
        try:
            row=correction.get(identifier)
            return perform(row['action_id'],CorrectionInput(fact=row['fact'],expected_version=row['target_version']-1),operation_id,identifier)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None

    return router
