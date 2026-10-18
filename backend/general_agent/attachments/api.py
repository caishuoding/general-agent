"""提供会话限定的附件上传、处理状态和原文件读取接口。"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse

from general_agent.attachments.service import AttachmentService
from general_agent.attachments.upload import receive_files


def attachment_router(service: AttachmentService) -> APIRouter:
    """构建独立资料接口；聊天仍由应用既有执行入口负责。"""
    router = APIRouter()

    @router.get('/api/conversations/{conversation_id}/attachments')
    def list_attachments(conversation_id: UUID) -> JSONResponse:
        """读取当前会话所有附件及真实处理状态。"""
        try:
            return JSONResponse(jsonable_encoder(service.list_attachments(conversation_id)))
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.post('/api/conversations/{conversation_id}/attachments', status_code=201)
    async def upload_attachments(conversation_id: UUID, request: Request, background: BackgroundTasks) -> JSONResponse:
        """完整收取有界上传后返回处理中状态，文本解析在后台执行。"""
        try:
            await asyncio.to_thread(service.list_attachments, conversation_id)
            uploaded = await receive_files(request, service.directory)
            rows = await asyncio.to_thread(service.register, conversation_id, uploaded)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        for row in rows:
            background.add_task(service.process, row['id'])
        return JSONResponse(jsonable_encoder(rows), status_code=201)

    @router.get('/api/conversations/{conversation_id}/attachments/{attachment_id}/file')
    def attachment_file(conversation_id: UUID, attachment_id: UUID) -> FileResponse:
        """授权后下载原文件，强制作为附件避免执行资料中的 HTML。"""
        try:
            path, filename = service.file(conversation_id, attachment_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        return FileResponse(path, media_type='application/octet-stream', filename=filename, headers={'X-Content-Type-Options': 'nosniff'})

    return router
