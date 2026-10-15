"""只按明确条目标识删除记忆，重复请求复用同一个无正文清理状态。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from general_agent.memory_deletion import MemoryDeletion


def deletion_router(deletion: MemoryDeletion) -> APIRouter:
    """管理入口直接调用正式删除边界，不将本地清理置于云端预算后。"""
    router=APIRouter()

    @router.delete('/api/memories/{action_id}')
    def delete_memory(action_id: UUID) -> JSONResponse:
        """完整清理后返回成功，失败明确提示停止召回且可重复本请求继续核对。"""
        try:
            return JSONResponse(jsonable_encoder(deletion.delete(action_id)))
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(503,str(error)) from None

    return router
