"""应用对外接口。"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from general_agent.chat import ChatService
from general_agent.database import Database
from general_agent.provider import ModelClient, ModelFailure
from general_agent.streaming import StreamingChat
from general_agent.settings import PublicSettings, SettingsInput, SettingsStore


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
    reply_to: UUID | None = None
    error: str | None = None


class ConversationDetail(Conversation):
    messages: list[Message] = Field(default_factory=list)


class UsageRecord(BaseModel):
    id: UUID
    conversation_id: UUID
    model: str
    status: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    created_at: datetime


def create_app(database_url: str | None = None, data_dir: Path | None = None, model_transport: httpx.BaseTransport | None = None) -> FastAPI:
    """创建应用，测试可指定真实数据库、配置目录和外部 HTTP 替身。"""
    database = Database(database_url or os.environ.get(
        "DATABASE_URL", "postgresql://general_agent:local-development-only@127.0.0.1:15432/general_agent"
    ))
    settings = SettingsStore(data_dir or Path(os.environ.get("APP_DATA_DIR", ".local")))
    chat = ChatService(database, ModelClient(model_transport))
    streaming = StreamingChat(database, ModelClient(model_transport))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """启动时迁移并恢复遗留执行；停机时标记未完成回答并短暂等待工作线程。"""
        database.migrate()
        streaming.recover()
        yield
        await asyncio.to_thread(streaming.close)

    app = FastAPI(title="个人对话智能体", lifespan=lifespan)
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
        """仅返回字段名和通用提示，避免验证错误回显密钥或完整请求体。"""
        fields = sorted({str(item["loc"][-1]) for item in error.errors()})
        return JSONResponse(status_code=422, content={"detail": "请检查输入字段：" + "、".join(fields)})

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

    @app.post("/api/conversations/{conversation_id}/messages", response_model=ConversationDetail)
    def send_message(conversation_id: UUID, body: MessageInput) -> ConversationDetail:
        """兼容同步对话并拒绝已有流式生成；失败保留记录并返回公开说明。"""
        saved = settings.read()
        if saved is None:
            raise HTTPException(409, "请先在设置中配置模型。")
        try:
            chat.send(conversation_id, body.content, saved)
        except LookupError:
            raise HTTPException(404, "会话不存在。") from None
        except ModelFailure as error:
            raise HTTPException(502, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return get_conversation(conversation_id)

    @app.post("/api/conversations/{conversation_id}/runs", status_code=202, response_model=ConversationDetail)
    def start_stream(conversation_id: UUID, body: MessageInput) -> ConversationDetail:
        """启动持久化的流式回答，客户端可订阅事件或重新读取会话。"""
        saved = settings.read()
        if saved is None:
            raise HTTPException(409, "请先在设置中配置模型。")
        try:
            streaming.start(conversation_id, body.content, saved)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return get_conversation(conversation_id)

    @app.post("/api/conversations/{conversation_id}/messages/{message_id}/retry", status_code=202, response_model=ConversationDetail)
    def retry_stream(conversation_id: UUID, message_id: UUID) -> ConversationDetail:
        """重试最新失败或停止的回答，产生新的实际调用而不重复用户消息。"""
        saved = settings.read()
        if saved is None:
            raise HTTPException(409, "请先在设置中配置模型。")
        try:
            streaming.retry(conversation_id, message_id, saved)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
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
        return [UsageRecord.model_validate(row) for row in database.list_usage()]

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app
