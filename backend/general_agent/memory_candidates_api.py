"""候选的明确批准入口，预算许可绑定候选正文、目标版本和嵌入配置。"""

from uuid import UUID
from typing import Literal
from pydantic import BaseModel, Field
from general_agent.attachment_candidate_source import clarify_subject
from general_agent.attachment_memory import AttachmentMemory, attachment_memory_intent

from fastapi import APIRouter, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from general_agent.budget import BudgetPaused
from general_agent.memory_candidates import MemoryCandidates, candidate_eligible
from general_agent.settings import SettingsStore


class SubjectInput(BaseModel):
    """人物澄清只允许选择本人或明确填写他人名称，不接受事实正文替换。"""
    kind: Literal['user','other']
    label: str = Field(default='',max_length=100)


def candidates_router(candidates: MemoryCandidates, settings_store: SettingsStore, attachments: AttachmentMemory) -> APIRouter:
    """接入明确确认、拒绝与来源重试；客户端不能替换候选正文或改取助手来源。"""
    router=APIRouter()
    budget=candidates.memory.calls.budget

    @router.post('/api/memory-candidates/{identifier}/confirm')
    def confirm_candidate(identifier: UUID, operation_id: UUID | None = Header(default=None,alias='X-Operation-ID')) -> JSONResponse:
        """仅确认此前展示的具体候选；预算恢复保留原操作，完成重放不重复写入。"""
        try:
            row=candidates.get(identifier)
            settings=candidates.memory.embedding.store.read()
            scope=[str(identifier),row['fact'],row['subject'],row['source_kind'],str(row['target_id']),row['target_version'],settings.revision if settings else None]
            with budget.operation('memory_candidate_confirm',scope,operation_id,row['source_conversation_id']) as (operation,previous):
                if previous and previous['result'] is not None:
                    return JSONResponse(previous['result'],status_code=previous['response_status'])
                try:
                    return JSONResponse(jsonable_encoder(candidates.confirm(identifier,operation)))
                except ValueError as error:
                    result={'detail':str(error)}
                    budget.cache_result(operation,result,409)
                    return JSONResponse(result,status_code=409)
        except BudgetPaused as paused:
            return JSONResponse(paused.public(),status_code=402)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @router.post('/api/memory-candidates/{identifier}/reject')
    def reject_candidate(identifier: UUID) -> JSONResponse:
        """本地拒绝具体未批准候选，不发出模型或嵌入请求。"""
        try:
            return JSONResponse(jsonable_encoder(candidates.reject(identifier)))
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @router.post('/api/conversations/{conversation_id}/messages/{message_id}/memory-candidates/retry')
    def retry_discovery(conversation_id: UUID, message_id: UUID, operation_id: UUID | None = Header(default=None,alias='X-Operation-ID')) -> JSONResponse:
        """显式重试原用户事实或已授权附件提取，绑定配置和来源，完成检查点不再次计费。"""
        try:
            source=candidates.source(message_id,conversation_id)
            settings=settings_store.read()
            if settings is None or not (candidate_eligible(source['content']) or attachment_memory_intent(source['content'])):
                raise ValueError('该来源不能自动提出个人候选，或聊天配置尚未保存。')
            with budget.operation('memory_candidate_discovery',[str(message_id),source['content'],settings.revision],operation_id,conversation_id) as (operation,previous):
                if previous and previous['result'] is not None:
                    return JSONResponse(previous['result'],status_code=previous['response_status'])
                if attachment_memory_intent(source['content']):
                    attachments.prepare(conversation_id,message_id,settings,operation)
                else:
                    candidates.discover(conversation_id,message_id,settings,operation)
                with candidates.database.connect() as connection:
                    state=connection.execute('SELECT status,error FROM memory_discoveries WHERE message_id=%s',(message_id,)).fetchone()
                status=200 if state and state['status']=='complete' else 409
                result={**state,'detail':state['error']} if state and status==409 else state
                budget.cache_result(operation,result,status)
                return JSONResponse(result,status_code=status)
        except BudgetPaused as paused:
            return JSONResponse(paused.public(),status_code=402)
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @router.put('/api/memory-candidates/{identifier}/subject')
    def subject_candidate(identifier: UUID, body: SubjectInput) -> JSONResponse:
        """人物选择只修改待确认预览，不发请求也不隐式批准保存。"""
        try:
            clarify_subject(candidates.database,candidates.memory.directory,identifier,body.kind,body.label)
            return JSONResponse(jsonable_encoder(candidates.get(identifier)))
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    return router
