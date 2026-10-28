"""账号认证、服务端会话和密码变更。"""

import hashlib
import secrets
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg

from general_agent.auth.passwords import hash_password, validate_new_password, verify_password
from general_agent.infrastructure.database import Database

SESSION_LIFETIME = timedelta(days=7)
USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")


@dataclass(frozen=True)
class AuthenticatedUser:
    """当前登录账号的最小安全视图。"""

    id: UUID
    username: str
    role: str
    must_change_password: bool


class AuthService:
    """封装账号验证、会话签发和密码变更。"""

    def __init__(self, database: Database, realm: str = "user") -> None:
        """绑定业务数据库和固定认证入口，不在构造时读取账号数据。"""
        if realm not in {"user", "admin"}:
            raise ValueError("认证入口无效。")
        self.database = database
        self.realm = realm
        self.account_table = "admin_accounts" if realm == "admin" else "users"
        self.session_table = "admin_sessions" if realm == "admin" else "sessions"

    def authenticate(self, username: str, password: str) -> AuthenticatedUser | None:
        """验证有效账号的用户名与密码，成功时更新最近登录时间。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, username, password_hash, role, status, must_change_password "
                f"FROM {self.account_table} WHERE username = %s AND role = %s",
                (username.strip().lower(), self.realm),
            ).fetchone()
            if row is None or row["status"] != "active":
                return None
            if not verify_password(row["password_hash"], password):
                return None
            connection.execute(
                f"UPDATE {self.account_table} SET last_login_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (row["id"],),
            )
            return self._view(row)

    def create_session(self, user_id: UUID) -> str:
        """签发不透明登录令牌，只在数据库保存其哈希。"""
        token = secrets.token_urlsafe(32)
        with self.database.connect() as connection:
            connection.execute(
                f"INSERT INTO {self.session_table} (id, user_id, token_hash, expires_at) "
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
                f"FROM {self.session_table} s JOIN {self.account_table} u ON u.id = s.user_id "
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
                f"DELETE FROM {self.session_table} WHERE token_hash = %s", (self._token_hash(token),)
            )

    def change_password(
        self, user: AuthenticatedUser, current_password: str, new_password: str
    ) -> str:
        """校验当前密码后更新密码、吊销旧会话并签发新会话。"""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT password_hash FROM {self.account_table} WHERE id = %s AND status = 'active'",
                (user.id,),
            ).fetchone()
            if row is None or not verify_password(row["password_hash"], current_password):
                raise ValueError("当前密码不正确。")
            if verify_password(row["password_hash"], new_password):
                raise ValueError("新密码不能与当前密码相同。")
            validate_new_password(new_password, user.username)
            connection.execute(
                f"UPDATE {self.account_table} SET password_hash = %s, must_change_password = FALSE, "
                "password_changed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (hash_password(new_password), user.id),
            )
            connection.execute(f"DELETE FROM {self.session_table} WHERE user_id = %s", (user.id,))
        return self.create_session(user.id)

    def list_users(self) -> list[dict[str, Any]]:
        """按创建时间列出账号公开字段，不返回密码哈希。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, username, role, status, must_change_password, "
                "created_at, updated_at, last_login_at FROM users WHERE role='user' "
                "ORDER BY created_at, id"
            ).fetchall()
        return [self._account_view(row) for row in rows]

    def get_user(self, user_id: UUID) -> dict[str, Any]:
        """按账号标识读取公开资料，不存在时抛出未找到。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, username, role, status, must_change_password, "
                "created_at, updated_at, last_login_at FROM users WHERE id = %s AND role='user'",
                (user_id,),
            ).fetchone()
        if row is None:
            raise LookupError("账号不存在。")
        return self._account_view(row)

    def create_user(self, username: str, password: str, role: str) -> dict[str, Any]:
        """由管理员创建必须首次改密的账号，用户名统一转小写并校验格式。"""
        normalized = username.strip().lower()
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("用户名需为 3–32 位小写字母、数字或下划线。")
        if self.realm != "user" or role != "user":
            raise ValueError("账号角色无效。")
        if not password:
            raise ValueError("请填写临时密码。")
        user_id = uuid4()
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    "INSERT INTO users (id, username, password_hash, role, status, must_change_password) "
                    "VALUES (%s, %s, %s, %s, 'active', TRUE) RETURNING "
                    "id, username, role, status, must_change_password, created_at, updated_at, last_login_at",
                    (user_id, normalized, hash_password(password), role),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise ValueError("用户名已存在。") from None
        assert row is not None
        return self._account_view(row)

    def update_user(self, user_id: UUID, status: str | None) -> dict[str, Any]:
        """仅更新普通用户账号状态，输入账号标识和状态并返回公开资料。"""
        with self.database.connect() as connection:
            target = connection.execute(
                "SELECT id FROM users WHERE id = %s AND role='user' FOR UPDATE",
                (user_id,),
            ).fetchone()
            if target is None:
                raise LookupError("账号不存在。")
            if status is not None and status not in {"active", "disabled"}:
                raise ValueError("账号状态无效。")
            row = connection.execute(
                "UPDATE users SET status=COALESCE(%s,status), "
                "updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING "
                "id, username, role, status, must_change_password, created_at, updated_at, last_login_at",
                (status, user_id),
            ).fetchone()
            if status == "disabled":
                connection.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
        assert row is not None
        return self._account_view(row)

    def reset_password(self, user_id: UUID) -> dict[str, Any]:
        """生成临时密码并重置普通用户账号，返回一次性展示的密码和公开资料。"""
        password = secrets.token_urlsafe(24)
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE users SET password_hash=%s, must_change_password=TRUE, "
                "password_changed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=%s AND role='user' "
                "RETURNING id, username, role, status, must_change_password, "
                "created_at, updated_at, last_login_at",
                (hash_password(password), user_id),
            ).fetchone()
            if row is None:
                raise LookupError("账号不存在。")
            connection.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
        return {**self._account_view(row), "temporary_password": password}

    def _view(self, row: dict[str, Any]) -> AuthenticatedUser:
        """把数据库行转换为不包含密码哈希的账号视图。"""
        return AuthenticatedUser(
            id=row["id"],
            username=row["username"],
            role=row["role"],
            must_change_password=bool(row["must_change_password"]),
        )

    def _account_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """序列化账号管理字段，避免密码哈希或会话信息进入响应。"""
        return {
            "id": str(row["id"]),
            "username": row["username"],
            "role": row["role"],
            "status": row["status"],
            "must_change_password": bool(row["must_change_password"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login_at": row["last_login_at"],
        }

    def _token_hash(self, token: str) -> str:
        """对浏览器令牌做单向摘要，降低数据库泄露后的会话风险。"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
