"""应用对外接口。"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from general_agent.attachment_api import attachment_router
from general_agent.attachments import AttachmentService
from general_agent.budget import BudgetInput, BudgetPaused, BudgetService, OperationContext
from general_agent.calls import CallGateway
from general_agent.capabilities import Capability, CapabilityResult, CapabilityService
from general_agent.chat import ChatService
from general_agent.execution import ChatExecution
from general_agent.memory import MemoryService
from general_agent.memory_candidates_api import candidates_router
from general_agent.memory_correction import MemoryCorrection
from general_agent.memory_correction_api import correction_router
from general_agent.database import Database
from general_agent.embedding import EmbeddingConfiguration, EmbeddingIndexConflict
from general_agent.provider import ModelClient, ModelFailure
from general_agent.streaming import StreamingChat
from general_agent.search import SearchService, SearchSettingsInput, SearchQueryInput, SearchDecisionInput
from general_agent.settings import (PublicSettings, SettingsInput, SettingsStore, EmbeddingSettingsInput, EmbeddingSettingsStore, PublicEmbeddingSettings)


class ConversationInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(default="新会话", min_length=1, max_length=120)


class Conversation(BaseModel):
    id: UUID
    title: str
    created_at: datetime


class MessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=32000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=5)


class Message(BaseModel):
    id: UUID
    role: str
    content: str
    status: str
    created_at: datetime
    reply_to: UUID | None = None
    error: str | None = None
    memory_candidates: list[dict[str, Any]] = Field(default_factory=list)
    memory_discovery: dict[str, Any] | None = None
    memory_action: dict[str, Any] | None = None
    memory_update: dict[str, Any] | None = None
    memory_recall: dict[str, Any] | None = None
    budget_pause: dict[str, str] | None = None
    attachment_ids: list[UUID] = Field(default_factory=list)
    search_action: dict[str, Any] | None = None
    web_sources: list[dict[str, object]] = Field(default_factory=list)
    citations: list[dict[str, object]] = Field(default_factory=list)


class ConversationDetail(Conversation):
    messages: list[Message] = Field(default_factory=list)


class ApprovalInput(BaseModel):
    pause_id: UUID


class UsageRecord(BaseModel):
    id: UUID
    conversation_id: UUID | None
    purpose: str
    model: str
    status: str
    operation_id: UUID | None = None
    base_url: str | None = None
    estimated_cost: str | None = None
    unit: str = "chat_tokens"
    credits: str | None = None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    created_at: datetime


def create_app(database_url: str | None = None, data_dir: Path | None = None, model_transport: httpx.BaseTransport | None = None) -> FastAPI:
    """创建聊天、资料及独立搜索应用，测试可指定数据库、目录和外部 HTTP 替身。"""
    database = Database(database_url or os.environ.get(
        "DATABASE_URL", "postgresql://general_agent:local-development-only@127.0.0.1:15432/general_agent"
    ))
    settings = SettingsStore(data_dir or Path(os.environ.get("APP_DATA_DIR", ".local")))
    embedding_settings = EmbeddingSettingsStore(settings.directory)
    embedding_configuration = EmbeddingConfiguration(database, embedding_settings)
    budget = BudgetService(database)
    model = CallGateway(budget, ModelClient(model_transport))
    memory = MemoryService(database, embedding_configuration, model, settings.directory)
    attachments = AttachmentService(database, settings.directory)
    search_settings = SettingsStore(settings.directory, 'search.json')
    search = SearchService(database, model, search_settings, memory, attachments, model_transport)
    execution = ChatExecution(model, memory, search)
    chat = ChatService(database, model, execution, attachments)
    capabilities = CapabilityService(database, model, settings, embedding_settings)
    streaming = StreamingChat(database, model, execution, attachments)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """启动时迁移并恢复遗留生成及资料处理；停机时标记未完成回答并等待工作线程。"""
        database.migrate()
        streaming.recover()
        attachments.recover()
        yield
        await asyncio.to_thread(streaming.close)

    app = FastAPI(title="个人对话智能体", lifespan=lifespan)
    app.include_router(attachment_router(attachments))
    app.include_router(correction_router(MemoryCorrection(memory)))
    app.include_router(candidates_router(execution.candidates,settings))
    app.state.calls = model
    app.state.budget = budget
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_origin_only(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """拒绝其他网站发起的浏览器请求，仅允许本机同源或本机开发界面。"""
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/"), "http://127.0.0.1:5173", "http://localhost:5173"}
        if origin and origin not in allowed:
            return JSONResponse(status_code=403, content={"detail": "请从本机应用页面访问。"})
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        """确认应用与实际数据库可访问，供本机启动检查使用。"""
        with database.connect() as connection:
            connection.execute("SELECT 1")
        return {"status": "ok"}

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        """返回字段或附件数量提示，避免验证错误回显密钥及完整请求体。"""
        if any("attachment_ids" in item["loc"] and item["type"] == "too_long" for item in error.errors()):
            return JSONResponse(status_code=422, content={"detail": "每条消息最多五个附件，请分成多条消息发送。"})
        fields = sorted({str(item["loc"][-1]) for item in error.errors()})
        return JSONResponse(status_code=422, content={"detail": "请检查输入字段：" + "、".join(fields)})

    @app.get('/api/settings/search', response_model=PublicSettings)
    def get_search_settings() -> PublicSettings:
        """展示独立 Tavily 配置状态，永不返回完整密钥。"""
        saved = search_settings.read()
        return saved.public() if saved else PublicSettings(base_url='https://api.tavily.com',model='tavily-basic')

    @app.put('/api/settings/search', response_model=PublicSettings)
    def save_search_settings(body: SearchSettingsInput) -> PublicSettings:
        """仅保存搜索连接，空密钥只在同地址保留，保存本身不联网。"""
        try:
            return search_settings.save(body)
        except ValueError:
            raise HTTPException(422,'请填写此搜索服务的密钥；配置未保存。') from None

    @app.delete('/api/settings/search', response_model=PublicSettings)
    def clear_search_settings() -> PublicSettings:
        """删除本机独立搜索凭据，聊天和嵌入配置仍可继续使用。"""
        with search_settings.lock:
            search_settings.path.unlink(missing_ok=True)
        return get_search_settings()

    @app.put('/api/conversations/{conversation_id}/search/{request_id}')
    def edit_search_query(conversation_id: UUID, request_id: UUID, body: SearchQueryInput) -> ConversationDetail:
        """修改待外发的完整文本并返回新修订，修改不构成批准。"""
        try:
            search.change_query(conversation_id,request_id,body)
            return get_conversation(conversation_id)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @app.post('/api/conversations/{conversation_id}/search/{request_id}/decision')
    def decide_search_query(conversation_id: UUID, request_id: UUID, body: SearchDecisionInput) -> ConversationDetail:
        """确认本次具体外发或拒绝发送；仅首次批准恢复原暂停答案。"""
        try:
            message_id=search.decide(conversation_id,request_id,body)
            if message_id:
                saved=settings.read()
                if saved is None:
                    raise ValueError('聊天配置不可用，请恢复配置后继续。')
                streaming.resume(conversation_id,message_id,saved)
            return get_conversation(conversation_id)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @app.get("/api/settings", response_model=PublicSettings)
    def get_settings() -> PublicSettings:
        """展示当前模型配置，密钥仅以是否已配置表示。"""
        saved = settings.read()
        return saved.public() if saved else PublicSettings()

    @app.put("/api/settings", response_model=PublicSettings)
    def save_settings(body: SettingsInput) -> PublicSettings:
        """保存模型配置并返回不含密钥的结果。"""
        try:
            return settings.save(body)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/settings/capabilities", response_model=dict[Capability, CapabilityResult])
    def get_capabilities() -> dict[Capability, CapabilityResult]:
        """展示当前已保存配置的独立能力结果，旧版本结果不会冒充当前能力。"""
        return capabilities.results()

    def perform(purpose: str, scope: object, operation_id: UUID | None, conversation_id: UUID | None, action: Callable[[OperationContext], BaseModel], status: int = 200) -> JSONResponse:
        """绑定请求范围并缓存已执行结果，暂停保留可恢复操作且不重复发送云端请求。"""
        try:
            with budget.operation(purpose, scope, operation_id, conversation_id) as (operation, previous):
                if previous and previous["result"] is not None:
                    return JSONResponse(previous["result"], status_code=previous["response_status"])
                if budget.has_calls(operation):
                    raise ValueError("该操作已执行但结果尚未确认，请先查看原会话或能力结果，不能重复发送。")
                try:
                    result = action(operation).model_dump(mode="json")
                    response_status = status
                except ModelFailure as error:
                    result, response_status = {"detail": str(error)}, 502
                budget.cache_result(operation, result, response_status)
                return JSONResponse(result, status_code=response_status)
        except BudgetPaused as paused:
            return JSONResponse(paused.public(), status_code=402)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    def perform_run(purpose: str, scope: object, operation_id: UUID | None, conversation_id: UUID, action: Callable[[OperationContext], None], status: int = 202) -> JSONResponse:
        """幂等受理仅关联稳定回答身份，初始 202 不会冻结后续预算批准或最终结果。"""
        try:
            with budget.operation(purpose, scope, operation_id, conversation_id) as (operation, previous):
                with database.connect() as connection:
                    existing = connection.execute('SELECT id FROM messages WHERE operation_id=%s', (operation.operation_id,)).fetchone()
                if existing is None:
                    action(operation)
                return JSONResponse(get_conversation(conversation_id).model_dump(mode='json'),status_code=status)
        except ModelFailure as error:
            return JSONResponse({'detail':str(error)},status_code=502)
        except BudgetPaused as paused:
            return JSONResponse(paused.public(),status_code=402)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @app.get('/api/memories')
    def list_memories() -> list[dict[str, Any]]:
        """展示明确批准事实、原始会话与消息来源，以及实际保存状态。"""
        try:
            return memory.list()
        except Exception:
            raise HTTPException(503,'记忆存储暂时不可读取，请检查 PostgreSQL/pgvector 后重试。') from None

    @app.post("/api/settings/capabilities/{capability}")
    def check_capability(capability: Capability, operation_id: UUID | None = Header(default=None, alias="X-Operation-ID")) -> JSONResponse:
        """将能力检查绑定到配置版本，暂停批准只恢复同一个能力和配置。"""
        saved = embedding_settings.read() if capability == "embedding" else settings.read()
        if saved is None:
            raise HTTPException(409, "请先保存对应的模型连接配置。")

        def check(operation: OperationContext) -> BaseModel:
            """用显式操作上下文执行当前能力检查。"""
            return capabilities.check(capability, operation, saved)
        return perform("capability_" + capability, saved.revision, operation_id, None, check)

    @app.get("/api/settings/budget")
    def get_budget() -> dict[str, object]:
        """展示人民币月度预算和按服务、模型绑定的单价。"""
        return budget.settings()

    @app.put("/api/settings/budget")
    def save_budget(body: BudgetInput) -> dict[str, object]:
        """保存可选月度预算，空预算允许继续使用并保留未知费用。"""
        return budget.save(body)

    @app.get("/api/usage/summary")
    def usage_summary() -> dict[str, object]:
        """返回上海时区当月全部调用统计，费用未知与已知部分分别展示。"""
        return budget.summary()

    @app.post("/api/budget/operations/{operation_id}/approve")
    def approve_operation(operation_id: UUID, body: ApprovalInput) -> dict[str, str]:
        """只批准持久化暂停挑战对应的下一次云端调用。"""
        try:
            budget.approve(operation_id, body.pause_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {"approval_scope": "next_call"}

    @app.post("/api/budget/operations/{operation_id}/cancel")
    def cancel_operation(operation_id: UUID) -> dict[str, str]:
        """撤回该操作后续调用许可，已经发生的用量仍可继续记录。"""
        budget.cancel(OperationContext(operation_id, ""))
        with database.connect() as connection:
            row = connection.execute('SELECT id,conversation_id FROM messages WHERE operation_id=%s',(operation_id,)).fetchone()
        if row:
            streaming.stop(row['conversation_id'],row['id'])
        return {"status": "cancelled"}

    @app.get("/api/settings/embedding", response_model=PublicEmbeddingSettings)
    def get_embedding_settings() -> PublicEmbeddingSettings:
        """读取独立嵌入配置，始终隐藏完整密钥。"""
        return embedding_configuration.public()

    @app.put("/api/settings/embedding", response_model=PublicEmbeddingSettings)
    def save_embedding_settings(body: EmbeddingSettingsInput) -> PublicEmbeddingSettings:
        """保存独立嵌入配置，已有索引时拒绝未经重建的向量空间变更。"""
        try:
            return embedding_configuration.save(body, settings.read())
        except EmbeddingIndexConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError:
            raise HTTPException(422, "请检查嵌入地址、密钥和维度；复用连接前请先保存聊天配置。") from None

    @app.post("/api/conversations", status_code=201, response_model=Conversation)
    def create_conversation(body: ConversationInput) -> Conversation:
        """为用户开启一段独立会话。"""
        return Conversation.model_validate(database.create_conversation(body.title))

    @app.get("/api/conversations", response_model=list[Conversation])
    def list_conversations() -> list[Conversation]:
        """返回可以继续访问的会话列表。"""
        return [Conversation.model_validate(row) for row in database.list_conversations()]

    @app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
    def get_conversation(conversation_id: UUID) -> ConversationDetail:
        """读取指定会话，不存在时返回明确的未找到状态。"""
        row = database.get_conversation(conversation_id)
        if row is None:
            raise HTTPException(404, "会话不存在。")
        return ConversationDetail.model_validate(row)

    @app.post("/api/conversations/{conversation_id}/messages")
    def send_message(conversation_id: UUID, body: MessageInput, operation_id: UUID | None = Header(default=None, alias="X-Operation-ID")) -> JSONResponse:
        """同步入口执行当前授权与已选附件回答；重复提交只读取原尝试。"""
        saved = settings.read()
        if saved is None:
            raise HTTPException(409, "请先在设置中配置模型。")

        def send(operation: OperationContext) -> None:
            """把本次附件与当前输入交给共同动作及资料执行流程。"""
            chat.send(conversation_id, body.content, saved, operation, body.attachment_ids)
        return perform_run("chat", [*execution.scope(body.content,saved), [str(item) for item in body.attachment_ids]], operation_id, conversation_id, send, 200)

    @app.post("/api/conversations/{conversation_id}/runs", status_code=202)
    def start_stream(conversation_id: UUID, body: MessageInput, operation_id: UUID | None = Header(default=None, alias="X-Operation-ID")) -> JSONResponse:
        """将附件身份绑定本次操作，只预留真实首步并由持久化快照恢复后续暂停。"""
        saved = settings.read()
        if saved is None:
            raise HTTPException(409, "请先在设置中配置模型。")

        def start(operation: OperationContext) -> None:
            """把操作与当前附件交给后台流程，使停止、恢复和用量仍属于同一执行。"""
            streaming.start(conversation_id, body.content, saved, operation, body.attachment_ids)
        return perform_run("chat_stream", [*execution.scope(body.content,saved), [str(item) for item in body.attachment_ids]], operation_id, conversation_id, start)

    @app.post("/api/conversations/{conversation_id}/messages/{message_id}/retry", status_code=202)
    def retry_stream(conversation_id: UUID, message_id: UUID, operation_id: UUID | None = Header(default=None, alias="X-Operation-ID")) -> JSONResponse:
        """重试构成新的可计量操作，每次实际云端请求重新检查预算。"""
        saved = settings.read()
        if saved is None:
            raise HTTPException(409, "请先在设置中配置模型。")

        def retry(operation: OperationContext) -> None:
            """仅为当前会话允许重试的原回答创建一个新尝试。"""
            streaming.retry(conversation_id, message_id, saved, operation)
        try:
            source = streaming.message(conversation_id,message_id)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        with database.connect() as connection:
            original = connection.execute('SELECT content FROM messages WHERE id=%s',(source['reply_to'],)).fetchone()
        scope = [str(message_id), execution.scope(str(original['content']) if original else '',saved)]
        return perform_run("chat_retry", scope, operation_id, conversation_id, retry)

    @app.post('/api/conversations/{conversation_id}/messages/{message_id}/resume', response_model=ConversationDetail)
    def resume_stream(conversation_id: UUID, message_id: UUID) -> ConversationDetail:
        """继续同一预算暂停步骤，读取当前配置但不挪用旧批准到新模型。"""
        saved = settings.read()
        if saved is None:
            raise HTTPException(409,'请先在设置中配置模型。')
        try:
            streaming.resume(conversation_id,message_id,saved)
        except ValueError as error:
            raise HTTPException(409,str(error)) from None
        return get_conversation(conversation_id)

    @app.post("/api/conversations/{conversation_id}/messages/{message_id}/stop", response_model=ConversationDetail)
    def stop_stream(conversation_id: UUID, message_id: UUID) -> ConversationDetail:
        """停止该次回答后续内容更新，已发出请求的用量仍可能继续取得。"""
        try:
            streaming.stop(conversation_id, message_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        return get_conversation(conversation_id)

    @app.get("/api/conversations/{conversation_id}/messages/{message_id}/events")
    def stream_events(conversation_id: UUID, message_id: UUID) -> StreamingResponse:
        """订阅已提交的回答快照；浏览器重连时从持久化记录恢复。"""
        try:
            streaming.message(conversation_id, message_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

        async def events() -> AsyncIterator[str]:
            """只在内容或状态变化时发送快照，并在终态关闭事件流。"""
            previous = ""
            while True:
                row = await asyncio.to_thread(streaming.message, conversation_id, message_id)
                message = Message.model_validate(row)
                current = message.model_dump_json()
                if current != previous:
                    yield "data: " + current + "\n\n"
                    previous = current
                if message.status != "generating":
                    return
                await asyncio.sleep(0.1)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/usage", response_model=list[UsageRecord])
    def get_usage() -> list[UsageRecord]:
        """返回最近调用的服务用量，未知值不会被替换为零。"""
        return [UsageRecord.model_validate({**row, "estimated_cost": format(row["estimated_cost"], "f") if row["estimated_cost"] is not None else None, "credits": format(row["credits"], "f") if row["credits"] is not None else None}) for row in database.list_usage()]

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app
