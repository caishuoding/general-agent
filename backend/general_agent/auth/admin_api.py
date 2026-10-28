"""管理员账号管理、跨账号只读查看和审计接口。"""

from datetime import date
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from general_agent.attachments.service import AttachmentService
from general_agent.auth.dependencies import require_admin
from general_agent.auth.service import AuthService, AuthenticatedUser
from general_agent.infrastructure.database import Database
from general_agent.knowledge.service import KnowledgeService
from general_agent.memory.service import MemoryService


class AdminUserInput(BaseModel):
    """管理员创建普通用户时提交的用户名和临时密码。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    role: Literal["user"] = "user"

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        """提前拒绝不符合账号命名规则的输入。"""
        if not value.isascii() or not all(char.isalnum() or char == "_" for char in value):
            raise ValueError("用户名需为 3–32 位小写字母、数字或下划线。")
        return value.lower()


class AdminUserPatch(BaseModel):
    """管理员只可修改普通用户状态。"""

    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled"] | None = None


def admin_router(
    auth: AuthService,
    database: Database,
    attachments: AttachmentService,
    memory: MemoryService,
    knowledge: KnowledgeService,
) -> APIRouter:
    """创建仅管理员可访问的账号管理和只读内容接口。"""
    router = APIRouter(prefix="/api/admin")

    @router.get("/users")
    def list_users(request: Request, query: str = "", status: Literal["active", "disabled"] | None = None) -> list[dict[str, Any]]:
        """按用户名和状态筛选普通用户账号公开资料。"""
        require_admin(request)
        rows = auth.list_users()
        needle = query.strip().lower()
        return [row for row in rows if (not needle or needle in row["username"]) and (status is None or row["status"] == status)]

    @router.post("/users", status_code=201)
    def create_user(body: AdminUserInput, request: Request) -> dict[str, Any]:
        """创建必须首次改密的普通用户。"""
        require_admin(request)
        try:
            return auth.create_user(body.username, body.password, "user")
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/users/{user_id}")
    def get_user(user_id: UUID, request: Request) -> dict[str, Any]:
        """读取指定账号的公开资料。"""
        require_admin(request)
        try:
            return auth.get_user(user_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None

    @router.patch("/users/{user_id}")
    def update_user(user_id: UUID, body: AdminUserPatch, request: Request) -> dict[str, Any]:
        """停用或启用普通用户账号。"""
        require_admin(request)
        try:
            return auth.update_user(user_id, body.status)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @router.post("/users/{user_id}/reset-password")
    def reset_password(user_id: UUID, request: Request) -> JSONResponse:
        """为普通用户生成一次性展示的临时密码并禁止响应缓存。"""
        require_admin(request)
        try:
            return JSONResponse(jsonable_encoder(auth.reset_password(user_id)), headers={"Cache-Control": "no-store"})
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/users/{user_id}/conversations")
    def user_conversations(user_id: UUID, request: Request, query: str = "", start: date | None = None, end: date | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """按标题、消息正文和日期查询目标账号会话，并记录访问审计。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        if offset < 0:
            raise HTTPException(422, "分页位置不能为负数。")
        needle = query.strip()
        with database.connect() as connection:
            # 正文命中标识用于前端打开会话后定位消息；标题或任一消息命中即可列出会话。
            # 日期按会话创建日闭区间筛选，分页由数据库完成，避免一次读取全部消息。
            rows = connection.execute(
                "SELECT c.id,c.title,c.created_at, "
                "(SELECT m.id FROM messages m WHERE m.conversation_id=c.id AND %s<>'' "
                "AND m.content ILIKE %s ORDER BY m.position LIMIT 1) AS matched_message_id "
                "FROM conversations c WHERE c.user_id=%s AND NOT c.deleting "
                "AND (%s='' OR c.title ILIKE %s OR EXISTS "
                "(SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND m.content ILIKE %s)) "
                "AND (%s::date IS NULL OR c.created_at::date >= %s::date) "
                "AND (%s::date IS NULL OR c.created_at::date <= %s::date) "
                "ORDER BY c.created_at DESC,c.id LIMIT 100 OFFSET %s",
                (needle, f"%{needle}%", user_id, needle, f"%{needle}%", f"%{needle}%", start, start, end, end, offset),
            ).fetchall()
        _audit(database, admin, user_id, "conversation_list", None, "view", request)
        return cast(list[dict[str, Any]], jsonable_encoder(rows))

    @router.get("/users/{user_id}/conversations/{conversation_id}")
    def user_conversation(
        user_id: UUID, conversation_id: UUID, request: Request
    ) -> dict[str, Any]:
        """只读查看目标账号的会话详情，不开放任何写入动作。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        row = database.get_conversation(conversation_id, user_id)
        if row is None:
            raise HTTPException(404, "会话不存在。")
        # 管理端只暴露当前仍有效的附件，避免旧消息带出已到期附件的信息或无效操作。
        with database.connect() as connection:
            valid_attachments = {
                item["id"] for item in connection.execute(
                    "SELECT id FROM attachments WHERE conversation_id=%s "
                    "AND status<>'deleting' AND expires_at>CURRENT_TIMESTAMP",
                    (conversation_id,),
                ).fetchall()
            }
        for message in row["messages"]:
            message["attachments"] = [item for item in message["attachments"] if item["id"] in valid_attachments]
        _audit(database, admin, user_id, "conversation", conversation_id, "view", request)
        return cast(dict[str, Any], jsonable_encoder(row))

    @router.get(
        "/users/{user_id}/conversations/{conversation_id}/attachments/{attachment_id}/file"
    )
    def user_attachment(
        user_id: UUID,
        conversation_id: UUID,
        attachment_id: UUID,
        request: Request,
    ) -> FileResponse:
        """只读下载目标账号的附件，并记录审计。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        try:
            path, filename = attachments.file(conversation_id, attachment_id, user_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        _audit(database, admin, user_id, "attachment", attachment_id, "download", request)
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=filename,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @router.get("/users/{user_id}/conversations/{conversation_id}/attachments/{attachment_id}/preview")
    def preview_attachment(user_id: UUID, conversation_id: UUID, attachment_id: UUID, request: Request) -> FileResponse:
        """只内联预览真实图片或 PDF 文件，并记录管理员查看审计。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        try:
            path, filename = attachments.file(conversation_id, attachment_id, user_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        with path.open("rb") as file:
            header = file.read(16)
        media_type = _preview_media_type(header)
        if media_type is None:
            raise HTTPException(415, "此附件不支持预览。")
        _audit(database, admin, user_id, "attachment", attachment_id, "preview", request)
        return FileResponse(path, media_type=media_type, filename=filename, content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})

    @router.get("/users/{user_id}/knowledge-bases/{base_id}/documents/{document_id}/file")
    def knowledge_document(user_id: UUID, base_id: UUID, document_id: UUID, request: Request) -> FileResponse:
        """只读下载目标用户仍有效的知识库文档并记录管理员审计。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        try:
            path, filename = knowledge.file(base_id, document_id, user_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        _audit(database, admin, user_id, "knowledge_document", document_id, "download", request)
        return FileResponse(path, media_type="application/octet-stream", filename=filename, headers={"X-Content-Type-Options": "nosniff"})

    @router.get("/users/{user_id}/knowledge-bases/{base_id}/documents/{document_id}/images/{image_id}/file")
    def knowledge_image(user_id: UUID, base_id: UUID, document_id: UUID, image_id: UUID, request: Request) -> FileResponse:
        """只读返回目标用户知识库引用原图，并记录管理员审计。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        try:
            path, media_type = knowledge.image_file(base_id, document_id, image_id, user_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        _audit(database, admin, user_id, "knowledge_image", image_id, "view", request)
        return FileResponse(path, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})

    @router.get("/users/{user_id}/memories")
    def user_memories(user_id: UUID, request: Request) -> list[dict[str, Any]]:
        """只读列出目标账号的长期记忆及来源状态。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        try:
            rows = memory.list(user_id)
        except Exception:
            raise HTTPException(503, "记忆存储暂时不可读取。") from None
        _audit(database, admin, user_id, "memory_list", None, "view", request)
        return cast(list[dict[str, Any]], jsonable_encoder(rows))

    @router.get("/users/{user_id}/search")
    def user_search(user_id: UUID, request: Request) -> list[dict[str, Any]]:
        """只读返回目标账号最近的搜索记录摘要。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT id, conversation_id, query, status, error, created_at "
                "FROM search_requests WHERE user_id=%s ORDER BY created_at DESC, id LIMIT 200",
                (user_id,),
            ).fetchall()
        _audit(database, admin, user_id, "search_list", None, "view", request)
        return cast(list[dict[str, Any]], jsonable_encoder(rows))

    @router.get("/users/{user_id}/usage")
    def user_usage(user_id: UUID, request: Request) -> list[dict[str, Any]]:
        """只读返回目标账号最近的调用用量。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        rows = database.list_usage(user_id)
        _audit(database, admin, user_id, "usage_list", None, "view", request)
        return cast(list[dict[str, Any]], jsonable_encoder(rows))

    @router.get("/audit")
    def audit_log(request: Request, target_user_id: UUID | None = None, action: str = "", start: date | None = None, end: date | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """按目标账号、动作和日期分页返回管理员访问审计。"""
        require_admin(request)
        if offset < 0:
            raise HTTPException(422, "分页位置不能为负数。")
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT id, admin_user_id, target_user_id, resource_type, resource_id, "
                "action, ip_address, created_at FROM admin_audit_log "
                "WHERE (%s::uuid IS NULL OR target_user_id=%s::uuid) "
                "AND (%s='' OR action=%s) "
                "AND (%s::date IS NULL OR created_at::date >= %s::date) "
                "AND (%s::date IS NULL OR created_at::date <= %s::date) "
                "ORDER BY created_at DESC, id LIMIT 100 OFFSET %s",
                (target_user_id, target_user_id, action, action, start, start, end, end, offset),
            ).fetchall()
        return cast(list[dict[str, Any]], jsonable_encoder(rows))

    return router


def _preview_media_type(header: bytes) -> str | None:
    """根据文件头识别允许预览的图片和 PDF，输入原始前缀并返回安全媒体类型。"""
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _ensure_target(auth: AuthService, user_id: UUID) -> None:
    """确认目标账号存在，避免跨账号接口继续查询不存在的范围。"""
    try:
        auth.get_user(user_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


def _audit(
    database: Database,
    admin: AuthenticatedUser,
    target_user_id: UUID,
    resource_type: str,
    resource_id: UUID | None,
    action: str,
    request: Request,
) -> None:
    """追加管理员查看普通用户内容的审计记录，输入目标资源和请求来源。"""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip_address = forwarded or (request.client.host if request.client else None)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO admin_audit_log "
            "(id, admin_user_id, target_user_id, resource_type, resource_id, action, ip_address) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                uuid4(),
                admin.id,
                target_user_id,
                resource_type,
                str(resource_id) if resource_id else None,
                action,
                ip_address,
            ),
        )
