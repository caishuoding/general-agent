"""提供长期记忆删除的 HTTP 适配器；删除动作本身仍由记忆服务负责。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from general_agent.memory.deletion import MemoryDeletion


def deletion_router(deletion: MemoryDeletion) -> APIRouter:
    """绑定长期记忆删除服务并提供可重复调用的清理入口。"""
    router = APIRouter()

    @router.delete("/api/memories/{action_id}")
    def delete_memory(action_id: UUID) -> JSONResponse:
        """按稳定记忆标识执行删除并返回清理状态。"""
        try:
            return JSONResponse(jsonable_encoder(deletion.delete(action_id)))
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(503, str(error)) from None

    return router
