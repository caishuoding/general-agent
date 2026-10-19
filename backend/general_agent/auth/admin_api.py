"""管理员账号管理、跨账号只读查看和审计接口。"""

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
from general_agent.memory.service import MemoryService


class AdminUserInput(BaseModel):
    """管理员创建账号时提交的用户名、临时密码和角色。"""

    model_config = ConfigDict(str_strip_whitespace=True)
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    role: Literal["admin", "user"] = "user"

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        """提前拒绝不符合账号命名规则的输入。"""
        if not value.isascii() or not all(char.isalnum() or char == "_" for char in value):
            raise ValueError("用户名需为 3–32 位小写字母、数字或下划线。")
        return value.lower()


class AdminUserPatch(BaseModel):
    """管理员可修改账号状态或角色，未提供字段保持原值。"""

    status: Literal["active", "disabled"] | None = None
    role: Literal["admin", "user"] | None = None


class ResetPasswordInput(BaseModel):
    """管理员重置账号使用的临时密码。"""

    password: str = Field(min_length=1, max_length=256)


def admin_router(
    auth: AuthService,
    database: Database,
    attachments: AttachmentService,
    memory: MemoryService,
) -> APIRouter:
    """创建仅管理员可访问的账号管理和只读内容接口。"""
    router = APIRouter(prefix="/api/admin")

    @router.get("/users")
    def list_users(request: Request) -> list[dict[str, Any]]:
        """列出所有账号的公开资料和状态。"""
        require_admin(request)
        return auth.list_users()

    @router.post("/users", status_code=201)
    def create_user(body: AdminUserInput, request: Request) -> dict[str, Any]:
        """创建必须首次改密的普通用户或管理员。"""
        require_admin(request)
        try:
            return auth.create_user(body.username, body.password, body.role)
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
        """停用、启用或调整账号角色，并保护最后一个有效管理员。"""
        require_admin(request)
        try:
            return auth.update_user(user_id, body.status, body.role)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @router.post("/users/{user_id}/reset-password")
    def reset_password(user_id: UUID, body: ResetPasswordInput, request: Request) -> dict[str, Any]:
        """重置临时密码并强制目标账号下次登录改密。"""
        require_admin(request)
        try:
            return auth.reset_password(user_id, body.password)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/users/{user_id}/conversations")
    def user_conversations(user_id: UUID, request: Request) -> list[dict[str, Any]]:
        """只读列出目标账号的会话，并记录管理员审计。"""
        admin = require_admin(request)
        _ensure_target(auth, user_id)
        rows = database.list_conversations(user_id)
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
    def audit_log(request: Request) -> list[dict[str, Any]]:
        """返回当前管理员最近的管理员审计记录。"""
        admin = require_admin(request)
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT id, admin_user_id, target_user_id, resource_type, resource_id, "
                "action, ip_address, created_at FROM admin_audit_log "
                "WHERE admin_user_id=%s ORDER BY created_at DESC, id LIMIT 200",
                (admin.id,),
            ).fetchall()
        return cast(list[dict[str, Any]], jsonable_encoder(rows))

    return router


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
    """追加管理员查看他人内容的审计记录，自身内容不重复记录。"""
    if admin.id == target_user_id:
        return
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
