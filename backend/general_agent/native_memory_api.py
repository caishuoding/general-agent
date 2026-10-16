"""只重试已有自动记忆来源，复用当前聊天模型和操作预算。"""

from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from general_agent.budget import BudgetPaused
from general_agent.native_memory import AutomaticMemory
from general_agent.settings import SettingsStore


def native_memory_router(automatic: AutomaticMemory, settings_store: SettingsStore) -> APIRouter:
    """提供失败自动记忆的独立重试，不要求重新发送或批准事实。"""
    router = APIRouter()
    memory = automatic.memory

    @router.post('/api/conversations/{conversation_id}/messages/{message_id}/memory/retry')
    def retry(conversation_id: UUID, message_id: UUID, operation_id: UUID | None = Header(default=None, alias='X-Operation-ID')) -> JSONResponse:
        """重试原始用户输入，模型配置变更使旧预算批准失效，停止或来源删除后不能复活。"""
        try:
            settings = settings_store.read()
            if settings is None:
                raise ValueError('请先配置聊天模型。')
            with memory.database.connect() as connection:
                source = connection.execute("SELECT m.content FROM messages m JOIN memory_native_runs r ON r.message_id=m.id JOIN conversations c ON c.id=m.conversation_id WHERE m.id=%s AND m.conversation_id=%s AND m.role='user' AND NOT c.deleting", (message_id, conversation_id)).fetchone()
            if source is None or memory.source_suppressed(message_id):
                raise ValueError('自动记忆来源不存在或已被忘记。')
            embedding = memory.embedding.store.read()
            with memory.calls.budget.operation('native_memory_retry', [str(message_id), settings.revision, embedding.revision if embedding else None], operation_id, conversation_id) as (operation, _):
                automatic.process(conversation_id, message_id, source['content'], settings, operation)
                with memory.database.connect() as connection:
                    state = connection.execute('SELECT status,detail FROM memory_native_runs WHERE message_id=%s', (message_id,)).fetchone()
                return JSONResponse(state, status_code=200 if state and state['status'] == 'complete' else 409)
        except BudgetPaused as paused:
            return JSONResponse(paused.public(), status_code=402)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    return router
