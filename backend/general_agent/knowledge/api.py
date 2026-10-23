"""知识库与会话选择的受保护 HTTP 接口。"""

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from general_agent.attachments.upload import receive_files
from general_agent.auth.dependencies import current_user
from general_agent.knowledge.service import KnowledgeService


class KnowledgeName(BaseModel):
    """校验知识库名称，拒绝空白和未知字段。"""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    name: str = Field(min_length=1, max_length=120)


class KnowledgeSelection(BaseModel):
    """一次替换当前会话选择，不允许客户端声明账号身份。"""
    model_config = ConfigDict(extra='forbid')
    knowledge_base_ids: list[UUID] = Field(max_length=20)


def knowledge_router(service: KnowledgeService) -> APIRouter:
    """注册独立资料管理接口，所有请求从登录会话取得账号。"""
    router = APIRouter()

    @router.get('/api/knowledge-bases')
    def list_bases(request: Request) -> list[dict[str, Any]]:
        """列出当前账号的知识库。"""
        return service.list_bases(current_user(request).id)

    @router.post('/api/knowledge-bases', status_code=201)
    def create_base(body: KnowledgeName, request: Request) -> dict[str, Any]:
        """创建当前账号的新知识库。"""
        return service.create(current_user(request).id, body.name)

    @router.patch('/api/knowledge-bases/{base_id}')
    def rename_base(base_id: UUID, body: KnowledgeName, request: Request) -> dict[str, Any]:
        """重命名自己的知识库，其他账号的对象返回不存在。"""
        try:
            return service.rename(base_id, current_user(request).id, body.name)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.delete('/api/knowledge-bases/{base_id}')
    def delete_base(base_id: UUID, request: Request) -> dict[str, str]:
        """先撤销知识库访问，再后台清理全部文档。"""
        try:
            service.delete(base_id, current_user(request).id)
            return {'status': 'deleting'}
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.get('/api/knowledge-bases/{base_id}/documents')
    def list_documents(base_id: UUID, request: Request) -> list[dict[str, Any]]:
        """返回文档及真实后台进度。"""
        try:
            return service.documents(base_id, current_user(request).id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.post('/api/knowledge-bases/{base_id}/documents', status_code=201)
    async def upload(base_id: UUID, request: Request) -> list[dict[str, Any]]:
        """有界上传与入库任务分离，响应不等待视觉或向量调用。"""
        account = current_user(request).id
        try:
            await asyncio.to_thread(service.documents, base_id, account)
            files = await receive_files(request, service.directory)
            rows = await asyncio.to_thread(service.register, base_id, account, files)
            for row in rows:
                service.schedule(row['id'])
            return rows
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.get('/api/knowledge-bases/{base_id}/documents/{document_id}/file')
    def download(base_id: UUID, document_id: UUID, request: Request) -> FileResponse:
        """权限核验后下载原件，强制附件模式避免执行文档内容。"""
        try:
            path, filename = service.file(base_id, document_id, current_user(request).id)
            return FileResponse(path, media_type='application/octet-stream', filename=filename,
                                headers={'X-Content-Type-Options': 'nosniff'})
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.post('/api/knowledge-bases/{base_id}/documents/{document_id}/retry')
    def retry(base_id: UUID, document_id: UUID, request: Request) -> dict[str, str]:
        """重试失败图片或索引，已有成功结果保留。"""
        try:
            service.retry(base_id, document_id, current_user(request).id)
            return {'status': 'processing'}
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @router.delete('/api/knowledge-bases/{base_id}/documents/{document_id}')
    def delete_document(base_id: UUID, document_id: UUID, request: Request) -> dict[str, str]:
        """立即停用指定文档，再后台清理其派生资料。"""
        try:
            service.delete(base_id, current_user(request).id, document_id)
            return {'status': 'deleting'}
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.get('/api/conversations/{conversation_id}/knowledge-bases')
    def selected(conversation_id: UUID, request: Request) -> list[dict[str, Any]]:
        """返回该账号会话的已选知识库。"""
        try:
            return service.selected(conversation_id, current_user(request).id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.put('/api/conversations/{conversation_id}/knowledge-bases')
    def select(conversation_id: UUID, body: KnowledgeSelection, request: Request) -> list[dict[str, Any]]:
        """原子替换已选知识库，空列表表示取消全部选择。"""
        try:
            return service.select(conversation_id, current_user(request).id, body.knowledge_base_ids)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    return router
