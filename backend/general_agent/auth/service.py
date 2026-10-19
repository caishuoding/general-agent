"""账号认证、服务端会话和密码变更。"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from general_agent.auth.passwords import hash_password, validate_new_password, verify_password
from general_agent.infrastructure.database import Database

SESSION_LIFETIME = timedelta(days=7)


@dataclass(frozen=True)
class AuthenticatedUser:
    """当前登录账号的最小安全视图。"""

    id: UUID
    username: str
    role: str
    must_change_password: bool


class AuthService:
    """封装账号验证、会话签发和密码变更。"""

    def __init__(self, database: Database) -> None:
        """绑定业务数据库，不在构造时读取账号数据。"""
        self.database = database

    def authenticate(self, username: str, password: str) -> AuthenticatedUser | None:
        """验证有效账号的用户名与密码，成功时更新最近登录时间。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, username, password_hash, role, status, must_change_password "
                "FROM users WHERE username = %s",
                (username.strip().lower(),),
            ).fetchone()
            if row is None or row["status"] != "active":
                return None
            if not verify_password(row["password_hash"], password):
                return None
            connection.execute(
                "UPDATE users SET last_login_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (row["id"],),
            )
            return self._view(row)

    def create_session(self, user_id: UUID) -> str:
        """签发不透明登录令牌，只在数据库保存其哈希。"""
        token = secrets.token_urlsafe(32)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO sessions (id, user_id, token_hash, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (uuid4(), user_id, self._token_hash(token), datetime.now(timezone.utc) + SESSION_LIFETIME),
            )
        return token

    def user_for_token(self, token: str | None) -> AuthenticatedUser | None:
        """按令牌读取仍有效且账号未停用的当前账号。"""
        if not token:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT u.id, u.username, u.role, u.must_change_password "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = %s AND s.expires_at > CURRENT_TIMESTAMP "
                "AND u.status = 'active'",
                (self._token_hash(token),),
            ).fetchone()
            return self._view(row) if row is not None else None

    def logout(self, token: str | None) -> None:
        """删除当前令牌对应的服务端会话。"""
        if not token:
            return
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = %s", (self._token_hash(token),)
            )

    def revoke_all_sessions(self, user_id: UUID) -> None:
        """吊销指定账号的全部登录会话，用于改密、重置和停用。"""
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))

    def change_password(
        self, user: AuthenticatedUser, current_password: str, new_password: str
    ) -> str:
        """校验当前密码后更新密码、吊销旧会话并签发新会话。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = %s AND status = 'active'",
                (user.id,),
            ).fetchone()
            if row is None or not verify_password(row["password_hash"], current_password):
                raise ValueError("当前密码不正确。")
            if verify_password(row["password_hash"], new_password):
                raise ValueError("新密码不能与当前密码相同。")
            validate_new_password(new_password, user.username)
            connection.execute(
                "UPDATE users SET password_hash = %s, must_change_password = FALSE, "
                "password_changed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (hash_password(new_password), user.id),
            )
            connection.execute("DELETE FROM sessions WHERE user_id = %s", (user.id,))
        return self.create_session(user.id)

    def _view(self, row: dict[str, Any]) -> AuthenticatedUser:
        """把数据库行转换为不包含密码哈希的账号视图。"""
        return AuthenticatedUser(
            id=row["id"],
            username=row["username"],
            role=row["role"],
            must_change_password=bool(row["must_change_password"]),
        )

    def _token_hash(self, token: str) -> str:
        """对浏览器令牌做单向摘要，降低数据库泄露后的会话风险。"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
