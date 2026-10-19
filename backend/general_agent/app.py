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

from general_agent.memory.source_deletion import SourceDeletion
from general_agent.memory.source_deletion_api import source_deletion_router
from general_agent.auth.api import COOKIE_NAME, auth_router
from general_agent.auth.admin_api import admin_router
from general_agent.auth.dependencies import current_user, require_admin
from general_agent.auth.service import AuthService, AuthenticatedUser
from general_agent.attachments.api import attachment_router
from general_agent.attachments.service import AttachmentService
from general_agent.infrastructure.operations import OperationContext, OperationService
from general_agent.infrastructure.calls import CallGateway
from general_agent.conversation.chat import ChatService
from general_agent.conversation.execution import ChatExecution
from general_agent.agent.conversation import ConversationGraph
from general_agent.memory.service import MemoryService
from general_agent.memory.automatic_api import native_memory_router
from general_agent.memory.correction import MemoryCorrection
from general_agent.memory.correction_api import correction_router
from general_agent.memory.deletion_api import deletion_router
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.logging import configure_agent_logging
from general_agent.memory.embedding import EmbeddingConfiguration
from general_agent.infrastructure.provider import ModelClient, ModelFailure
from general_agent.conversation.streaming import StreamingChat
from general_agent.search.service import SearchService, SearchSettingsInput
from general_agent.infrastructure.settings import (PublicSettings, SettingsInput, SettingsStore, EmbeddingSettingsInput, EmbeddingSettingsStore, PublicEmbeddingSettings)


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


class Message(BaseModel):
    id: UUID
    role: str
    content: str
    status: str
    created_at: datetime
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    reply_to: UUID | None = None
    error: str | None = None
    automatic_memory: dict[str, Any] | None = None
    memory_action: dict[str, Any] | None = None
    memory_update: dict[str, Any] | None = None
    memory_recall: dict[str, Any] | None = None
    search_action: dict[str, Any] | None = None
    web_sources: list[dict[str, object]] = Field(default_factory=list)


class ConversationDetail(Conversation):
    messages: list[Message] = Field(default_factory=list)


class UsageRecord(BaseModel):
    id: UUID
    conversation_id: UUID | None
    purpose: str
    model: str
    status: str
    operation_id: UUID | None = None
    base_url: str | None = None
    unit: str = "chat_tokens"
    credits: str | None = None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    created_at: datetime


def create_app(database_url: str | None = None, data_dir: Path | None = None, model_transport: httpx.BaseTransport | None = None, mem0_transport: httpx.BaseTransport | None = None, secure_cookies: bool | None = None) -> FastAPI:
    """创建聊天、自动记忆和资料应用，测试可指定隔离数据库、目录及模型 HTTP 替身。"""
    database = Database(database_url or os.environ.get(
        "DATABASE_URL", "postgresql://general_agent:local-development-only@127.0.0.1:15432/general_agent"
    ))
    cookie_secure = (
        secure_cookies
        if secure_cookies is not None
        else os.environ.get("COOKIE_SECURE", "false").lower() == "true"
    )
    auth = AuthService(database)
    settings = SettingsStore(data_dir or Path(os.environ.get("APP_DATA_DIR", ".local")))
    configure_agent_logging(settings.directory)
    embedding_settings = EmbeddingSettingsStore(settings.directory)
    embedding_configuration = EmbeddingConfiguration(database, embedding_settings)
    operations = OperationService(database)
    model = CallGateway(operations, ModelClient(model_transport))
    memory = MemoryService(database, embedding_configuration, model, settings.directory, mem0_transport)
    attachments = AttachmentService(database, settings.directory)
    search_settings = SettingsStore(settings.directory, 'search.json')
    search = SearchService(database, model, search_settings, memory, attachments, model_transport)
    execution = ChatExecution(model, memory, search)
    conversation_graph = ConversationGraph(database, model, execution, attachments)
    source_deletion = SourceDeletion(execution.deletion, memory, conversation_graph.delete_threads)
    chat = ChatService(database, model, execution, attachments, conversation_graph)
    streaming = StreamingChat(database, model, execution, attachments, conversation_graph)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """迁移数据库并恢复生成、附件与来源删除，停机时等待工作线程。"""
        database.migrate()
        conversation_graph.start()
        streaming.recover()
        attachments.recover()
        source_deletion.recover()
        yield
        await asyncio.to_thread(streaming.close)
        conversation_graph.close()

    app = FastAPI(title="个人对话智能体", lifespan=lifespan)
    app.include_router(auth_router(auth, cookie_secure))
    app.include_router(admin_router(auth, database, attachments, memory))
    app.include_router(attachment_router(attachments))
    app.include_router(source_deletion_router(source_deletion))
    app.include_router(correction_router(MemoryCorrection(memory)))
    app.include_router(native_memory_router(execution.automatic,settings))
    app.include_router(deletion_router(execution.deletion))
    app.state.calls = model
    app.state.operations = operations
    app.state.auth = auth
    trusted_hosts = ["127.0.0.1", "localhost", "[::1]"]
    public_host = os.environ.get("PUBLIC_HOST", "").strip()
    if public_host:
        trusted_hosts.append(public_host)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

    @app.get("/api/memories")
    def list_memories(request: Request) -> list[dict[str, Any]]:
        """提供存储核验接口，供自动化测试和外部数据库管理工具使用。"""
        admin = require_admin(request)
        try:
            return memory.list(admin.id)
        except Exception:
            raise HTTPException(503, "记忆存储暂时不可读取，请检查 Mem0 服务后重试。") from None

    @app.middleware("http")
    async def local_origin_only(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """拒绝其他网站发起的浏览器请求，仅允许本机同源或本机开发界面。"""
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/"), "http://127.0.0.1:5173", "http://localhost:5173"}
        if origin and origin not in allowed:
            return JSONResponse(status_code=403, content={"detail": "请从本机应用页面访问。"})
        return await call_next(request)

    @app.middleware("http")
    async def require_login(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """保护业务接口，并在必须改密时只放行账号与密码相关接口。"""
        path = request.url.path
        public = path in {"/api/health", "/api/auth/login"} or not path.startswith("/api/")
        if public:
            return await call_next(request)
        user = auth.user_for_token(request.cookies.get(COOKIE_NAME))
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "请先登录。"})
        request.state.user = user
        if user.must_change_password and path not in {
            "/api/auth/me",
            "/api/auth/logout",
            "/api/auth/password",
        }:
            return JSONResponse(status_code=403, content={"detail": "请先修改密码。"})
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        """确认应用与实际数据库可访问，供本机启动检查使用。"""
        with database.connect() as connection:
            connection.execute("SELECT 1")
        return {"status": "ok"}

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        """返回字段提示，避免验证错误回显密钥及完整请求体。"""
        fields = sorted({str(item["loc"][-1]) for item in error.errors()})
        return JSONResponse(status_code=422, content={"detail": "请检查输入字段：" + "、".join(fields)})

    @app.get('/api/settings/search', response_model=PublicSettings)
    def get_search_settings(request: Request) -> PublicSettings:
        """展示独立智谱搜索配置状态，旧供应商凭据不会被误当作可用。"""
        require_admin(request)
        saved = search_settings.read()
        return saved.public() if saved and saved.model == 'zhipu-search-std' else PublicSettings(base_url='https://open.bigmodel.cn/api/paas/v4',model='zhipu-search-std')

    @app.put('/api/settings/search', response_model=PublicSettings)
    def save_search_settings(body: SearchSettingsInput, request: Request) -> PublicSettings:
        """仅保存搜索连接，空密钥只在同地址保留，保存本身不联网。"""
        require_admin(request)
        try:
            return search_settings.save(body)
        except ValueError:
            raise HTTPException(422,'请填写此搜索服务的密钥；配置未保存。') from None

    @app.delete('/api/settings/search', response_model=PublicSettings)
    def clear_search_settings(request: Request) -> PublicSettings:
        """删除本机独立搜索凭据，聊天和嵌入配置仍可继续使用。"""
        require_admin(request)
        with search_settings.lock:
            search_settings.path.unlink(missing_ok=True)
        return get_search_settings(request)

    @app.get("/api/settings", response_model=PublicSettings)
    def get_settings(request: Request) -> PublicSettings:
        """展示当前模型配置，密钥仅以是否已配置表示。"""
        require_admin(request)
        saved = settings.read()
        return saved.public() if saved else PublicSettings()

    @app.put("/api/settings", response_model=PublicSettings)
    def save_settings(body: SettingsInput, request: Request) -> PublicSettings:
        """保存模型配置并返回不含密钥的结果。"""
        require_admin(request)
        try:
            return settings.save(body)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    def perform_run(purpose: str, scope: object, operation_id: UUID | None, conversation_id: UUID, user_id: UUID, action: Callable[[OperationContext], None], status: int = 202) -> JSONResponse:
        """幂等受理仅关联稳定回答身份，重复提交不会创建第二次执行。"""
        try:
            with operations.operation(purpose, scope, operation_id, conversation_id, user_id) as (operation, previous):
                with database.connect() as connection:
                    existing = connection.execute('SELECT id FROM messages WHERE operation_id=%s', (operation.operation_id,)).fetchone()
                if existing is None:
                    action(operation)
                return JSONResponse(read_conversation(conversation_id, user_id).model_dump(mode='json'),status_code=status)
        except ModelFailure as error:
            return JSONResponse({'detail':str(error)},status_code=502)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        except ValueError as error:
            raise HTTPException(409,str(error)) from None

    @app.get("/api/settings/embedding", response_model=PublicEmbeddingSettings)
    def get_embedding_settings(request: Request) -> PublicEmbeddingSettings:
        """读取独立嵌入配置，始终隐藏完整密钥。"""
        require_admin(request)
        return embedding_configuration.public()

    @app.put("/api/settings/embedding", response_model=PublicEmbeddingSettings)
    def save_embedding_settings(body: EmbeddingSettingsInput, request: Request) -> PublicEmbeddingSettings:
        """保存 Mem0 嵌入配置；连接错误由后续实际记忆调用反馈。"""
        require_admin(request)
        try:
            return embedding_configuration.save(body, settings.read())
        except ValueError:
            raise HTTPException(422, "请检查嵌入地址、密钥和维度；复用连接前请先保存聊天配置。") from None

    @app.post("/api/conversations", status_code=201, response_model=Conversation)
    def create_conversation(body: ConversationInput, request: Request) -> Conversation:
        """为用户开启一段独立会话。"""
        return Conversation.model_validate(database.create_conversation(body.title, current_user(request).id))

    @app.get("/api/conversations", response_model=list[Conversation])
    def list_conversations(request: Request) -> list[Conversation]:
        """返回可以继续访问的会话列表。"""
        return [Conversation.model_validate(row) for row in database.list_conversations(current_user(request).id)]

    def read_conversation(conversation_id: UUID, user_id: UUID) -> ConversationDetail:
        """按账号读取指定会话，不存在时返回明确的未找到状态。"""
        row = database.get_conversation(conversation_id, user_id)
        if row is None:
            raise HTTPException(404, "会话不存在。")
        return ConversationDetail.model_validate(row)

    @app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
    def get_conversation(conversation_id: UUID, request: Request) -> ConversationDetail:
        """读取当前账号的指定会话。"""
        return read_conversation(conversation_id, current_user(request).id)

    @app.post("/api/conversations/{conversation_id}/messages")
    def send_message(conversation_id: UUID, body: MessageInput, request: Request, operation_id: UUID | None = Header(default=None, alias="X-Operation-ID")) -> JSONResponse:
        """同步入口执行当前授权和完整会话附件回答；重复提交只读取原尝试。"""
        account_id = current_user(request).id
        saved = settings.read()
        if saved is None:
            raise HTTPException(409, "请先在设置中配置模型。")

        def send(operation: OperationContext) -> None:
            """把当前输入交给共同动作及完整附件执行流程。"""
            chat.send(conversation_id, account_id, body.content, saved, operation)
        return perform_run("chat", execution.scope(body.content,saved), operation_id, conversation_id, account_id, send, 200)

    @app.post("/api/conversations/{conversation_id}/runs", status_code=202)
    def start_stream(conversation_id: UUID, body: MessageInput, request: Request, operation_id: UUID | None = Header(default=None, alias="X-Operation-ID")) -> JSONResponse:
        """登记本次操作并启动后台回答，失败、停止和恢复仍沿用同一执行身份。"""
        account_id = current_user(request).id
        saved = settings.read()
        if saved is None:
            raise HTTPException(409, "请先在设置中配置模型。")

        def start(operation: OperationContext) -> None:
            """把操作交给后台流程，使停止、恢复和用量仍属于同一执行。"""
            streaming.start(conversation_id, account_id, body.content, saved, operation)
        return perform_run("chat_stream", execution.scope(body.content,saved), operation_id, conversation_id, account_id, start)

    @app.post("/api/conversations/{conversation_id}/messages/{message_id}/retry", status_code=202)
    def retry_stream(conversation_id: UUID, message_id: UUID, request: Request, operation_id: UUID | None = Header(default=None, alias="X-Operation-ID")) -> JSONResponse:
        """重试构成新的可计量操作，每次实际云端请求单独记录用量。"""
        account_id = current_user(request).id
        saved = settings.read()
        if saved is None:
            raise HTTPException(409, "请先在设置中配置模型。")

        def retry(operation: OperationContext) -> None:
            """仅为当前会话允许重试的原回答创建一个新尝试。"""
            streaming.retry(conversation_id, account_id, message_id, saved, operation)
        try:
            source = streaming.message(conversation_id, account_id, message_id)
        except LookupError as error:
            raise HTTPException(404,str(error)) from None
        with database.connect() as connection:
            original = connection.execute('SELECT content FROM messages WHERE id=%s',(source['reply_to'],)).fetchone()
        scope = [str(message_id), execution.scope(str(original['content']) if original else '',saved)]
        return perform_run("chat_retry", scope, operation_id, conversation_id, account_id, retry)

    @app.post("/api/conversations/{conversation_id}/messages/{message_id}/stop", response_model=ConversationDetail)
    def stop_stream(conversation_id: UUID, message_id: UUID, request: Request) -> ConversationDetail:
        """停止该次回答后续内容更新，已发出请求的用量仍可能继续取得。"""
        account_id = current_user(request).id
        try:
            streaming.stop(conversation_id, account_id, message_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        return read_conversation(conversation_id, account_id)

    @app.get("/api/conversations/{conversation_id}/messages/{message_id}/events")
    def stream_events(conversation_id: UUID, message_id: UUID, request: Request) -> StreamingResponse:
        """订阅已提交的回答快照；浏览器重连时从持久化记录恢复。"""
        account_id = current_user(request).id
        try:
            streaming.message(conversation_id, account_id, message_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

        async def events() -> AsyncIterator[str]:
            """只在内容或状态变化时发送快照，并在终态关闭事件流。"""
            previous = ""
            while True:
                row = await asyncio.to_thread(streaming.message, conversation_id, account_id, message_id)
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
    def get_usage(request: Request) -> list[UsageRecord]:
        """返回最近调用的状态和服务商提供的实际用量。"""
        return [UsageRecord.model_validate({**row, "credits": format(row["credits"], "f") if row["credits"] is not None else None}) for row in database.list_usage(current_user(request).id)]

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app
