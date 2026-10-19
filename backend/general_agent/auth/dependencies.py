"""从请求状态读取登录账号并提供管理员校验。"""

from fastapi import HTTPException, Request

from general_agent.auth.service import AuthenticatedUser


def current_user(request: Request) -> AuthenticatedUser:
    """返回认证中间件注入的当前账号，缺失时要求重新登录。"""
    user = getattr(request.state, "user", None)
    if not isinstance(user, AuthenticatedUser):
        raise HTTPException(401, "请先登录。")
    return user


def require_admin(request: Request) -> AuthenticatedUser:
    """要求当前账号为管理员，普通用户访问后台时返回禁止访问。"""
    user = current_user(request)
    if user.role != "admin":
        raise HTTPException(403, "只有管理员可以执行此操作。")
    return user
