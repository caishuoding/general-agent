"""来源删除只接受核对后的影响版本与明确保留条目。"""

from uuid import UUID
from typing import Any
from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from general_agent.memory.source_deletion import SourceDeletion


class SourceDecision(BaseModel):
    """默认删除关联记忆，保留必须由用户逐项明确指定。"""
    preview_revision: str
    keep_memory_ids: list[UUID] = Field(default_factory=list)


def source_deletion_router(service: SourceDeletion) -> APIRouter:
    """提供可恢复的来源影响与清理状态，所有写入由同一个领域边界负责。"""
    router=APIRouter()

    def preview(kind: str, conversation_id: UUID, source_id: UUID) -> JSONResponse:
        """把真实影响及缺失来源映射为公开结果。"""
        try:
            return JSONResponse(jsonable_encoder(service.preview(kind,conversation_id,source_id)))
        except LookupError as error:
            raise HTTPException(404,str(error)) from None

    def remove(kind: str, conversation_id: UUID, source_id: UUID, body: SourceDecision) -> JSONResponse:
        """只有最新影响批准才接受删除，部分失败返回可恢复身份而不声称完成。"""
        try:
            result=service.accept(kind,conversation_id,source_id,body.preview_revision,body.keep_memory_ids)
            return JSONResponse(jsonable_encoder(result),status_code=200 if result['status']=='complete' else 503)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @router.get('/api/conversations/{conversation_id}/attachments/{attachment_id}/deletion-preview')
    def attachment_preview(conversation_id: UUID, attachment_id: UUID) -> JSONResponse:
        """预览单个独立上传的关联事实，不按同名文件扩大范围。"""
        return preview('attachment',conversation_id,attachment_id)

    @router.delete('/api/conversations/{conversation_id}/attachments/{attachment_id}')
    def delete_attachment(conversation_id: UUID, attachment_id: UUID, body: SourceDecision) -> JSONResponse:
        """删除已核对的当前会话附件并处理其关联记忆。"""
        return remove('attachment',conversation_id,attachment_id,body)

    @router.get('/api/conversations/{conversation_id}/deletion-preview')
    def conversation_preview(conversation_id: UUID) -> JSONResponse:
        """列出会话直接保存、自动记忆及更正来源的全部关联记忆。"""
        return preview('conversation',conversation_id,conversation_id)

    @router.delete('/api/conversations/{conversation_id}')
    def delete_conversation(conversation_id: UUID, body: SourceDecision) -> JSONResponse:
        """清理整会话及附件，明确保留的正式记忆解除来源后继续有效。"""
        return remove('conversation',conversation_id,conversation_id,body)

    @router.get('/api/source-deletions')
    def pending_deletions() -> list[dict[str,Any]]:
        """读取未完成的无正文删除身份，来源已隐藏时刷新仍能继续清理。"""
        with service.database.connect() as connection:
            return connection.execute("SELECT id,kind,source_id,status,phase,error FROM source_deletions WHERE status<>'complete' ORDER BY created_at").fetchall()

    @router.get('/api/source-deletions/{identifier}')
    def deletion_status(identifier: UUID) -> dict[str,Any]:
        """来源已物理删除后仍可查询不含正文的持久清理状态。"""
        try:
            return service.result(identifier)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None

    @router.post('/api/source-deletions/{identifier}/retry')
    def retry_deletion(identifier: UUID) -> JSONResponse:
        """只恢复原批准清理，不能通过重试改选保留记忆。"""
        try:
            result=service.resume(identifier)
            return JSONResponse(jsonable_encoder(result),status_code=200 if result['status']=='complete' else 503)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None

    return router
