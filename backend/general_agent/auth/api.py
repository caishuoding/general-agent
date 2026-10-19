"""登录、退出、当前账号和修改密码接口。"""

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from general_agent.auth.service import AuthService, AuthenticatedUser

COOKIE_NAME = "general_agent_session"


class LoginInput(BaseModel):
    """登录表单，用户名和密码都去除首尾空白。"""

    model_config = ConfigDict(str_strip_whitespace=True)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeInput(BaseModel):
    """修改密码表单，当前密码只用于本次校验。"""

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


def auth_router(service: AuthService, secure_cookies: bool) -> APIRouter:
    """创建认证路由，Cookie 安全属性由部署环境决定。"""
    router = APIRouter(prefix="/api/auth")

    @router.post("/login")
    def login(body: LoginInput, request: Request, response: Response) -> dict[str, Any]:
        """验证用户名密码并签发登录会话，初始管理员首次改密前只允许本机。"""
        user = service.authenticate(body.username, body.password)
        if user is None:
            raise HTTPException(401, "用户名或密码错误。")
        if user.username == "admin" and user.must_change_password and not _direct_local_request(request):
            raise HTTPException(403, "请在服务器本机完成首次改密后再从公网登录。")
        token = service.create_session(user.id)
        _set_session_cookie(response, token, _cookie_secure(request, secure_cookies))
        return _public_user(user)

    @router.post("/logout")
    def logout(request: Request, response: Response) -> dict[str, str]:
        """删除当前服务端会话并清除浏览器 Cookie。"""
        service.logout(request.cookies.get(COOKIE_NAME))
        response.delete_cookie(
            COOKIE_NAME,
            path="/",
            secure=_cookie_secure(request, secure_cookies),
            httponly=True,
            samesite="lax",
        )
        return {"status": "ok"}

    @router.get("/me")
    def me(request: Request) -> dict[str, Any]:
        """返回当前账号，不包含密码哈希或会话令牌。"""
        user = _request_user(request)
        return _public_user(user)

    @router.post("/password")
    def change_password(body: PasswordChangeInput, request: Request, response: Response) -> dict[str, Any]:
        """修改自己的密码并替换当前浏览器会话。"""
        user = _request_user(request)
        try:
            token = service.change_password(user, body.current_password, body.new_password)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        _set_session_cookie(response, token, _cookie_secure(request, secure_cookies))
        updated = service.user_for_token(token)
        assert updated is not None
        return _public_user(updated)

    return router


def _request_user(request: Request) -> AuthenticatedUser:
    """从认证中间件保存的请求状态读取当前账号。"""
    user = getattr(request.state, "user", None)
    if not isinstance(user, AuthenticatedUser):
        raise HTTPException(401, "请先登录。")
    return user


def _public_user(user: AuthenticatedUser) -> dict[str, Any]:
    """序列化账号视图，只暴露前端需要的字段。"""
    return {**asdict(user), "id": str(user.id)}


def _set_session_cookie(response: Response, token: str, secure: bool) -> None:
    """写入 HttpOnly、SameSite=Lax 的 7 天登录 Cookie。"""
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=7 * 24 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _direct_local_request(request: Request) -> bool:
    """识别没有反向代理来源头的本机或 SSH 隧道请求。"""
    if request.headers.get("x-forwarded-for"):
        return False
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def _cookie_secure(request: Request, configured: bool) -> bool:
    """HTTPS 或可信反向代理转发时必须使用 Secure，SSH 隧道本机 HTTP 引导除外。"""
    return configured or request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
