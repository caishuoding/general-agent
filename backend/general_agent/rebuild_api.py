"""明确目标预览、后台重建和同一任务恢复的公开接口。"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from general_agent.rebuild import EmbeddingRebuild
from general_agent.settings import EmbeddingSettingsInput, SettingsStore


class RebuildInput(BaseModel):
    id: UUID
    target: EmbeddingSettingsInput
    confirmed: bool = False


def rebuild_router(service: EmbeddingRebuild, settings: SettingsStore) -> APIRouter:
    """路由只转换公开输入；预算和持久工作在重建模块内统一处理。"""
    router=APIRouter(prefix='/api/settings/embedding')

    @router.post('/rebuild-impact')
    def preview(body: EmbeddingSettingsInput) -> dict[str, Any]:
        """返回目标与真实受影响范围，预览不产生任何云请求。"""
        try:
            return service.impact(body,settings.read())
        except ValueError:
            raise HTTPException(422,'请检查目标嵌入地址、模型、维度和密钥。') from None

    @router.post('/rebuilds',status_code=202)
    def start(body: RebuildInput) -> dict[str, Any]:
        """受理明确批准的稳定任务，重复身份不能指向其他目标。"""
        try:
            return service.start(body.id,body.target,settings.read(),body.confirmed)
        except ValidationError:
            raise HTTPException(422,'请检查目标嵌入地址、模型、维度和密钥。') from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @router.get('/rebuilds/latest')
    def latest() -> dict[str, Any] | None:
        """设置页重新打开时恢复最近任务状态。"""
        return service.latest()

    @router.get('/rebuilds/{identifier}')
    def get(identifier: UUID) -> dict[str, Any]:
        """返回真实进度和安全错误，不返回事实或目标密钥。"""
        try:
            return service.public(identifier)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None

    @router.post('/rebuilds/{identifier}/resume',status_code=202)
    def resume(identifier: UUID) -> dict[str, Any]:
        """只继续原批准目标，已完成工作不因按钮重复点击而重新收费。"""
        try:
            return service.resume(identifier)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None

    @router.post('/rebuilds/{identifier}/cancel',status_code=202)
    def cancel(identifier: UUID) -> dict[str, Any]:
        """先撤回后续调用再后台清理目标，完成前保持真实 cancelling 状态。"""
        try:
            return service.cancel(identifier)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    return router
