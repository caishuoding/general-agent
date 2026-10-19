"""密码哈希、校验和强度检查。"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

BOOTSTRAP_ADMIN_PASSWORD = "123456"

COMMON_PASSWORDS = {
    "123456",
    "12345678",
    "123456789",
    "password",
    "password1",
    "qwerty",
    "qwerty123",
    "admin",
    "admin123",
    "letmein",
    "welcome",
}

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """使用 Argon2id 生成密码哈希，数据库中不保存明文。"""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验明文密码与哈希是否匹配，哈希损坏时按不匹配处理。"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def validate_new_password(password: str, username: str) -> None:
    """校验新密码长度、用户名冲突和常见弱口令，不满足时抛出中文错误。"""
    normalized = password.strip()
    if len(normalized) < 10:
        raise ValueError("新密码至少需要 10 个字符。")
    if normalized.lower() == username.lower():
        raise ValueError("新密码不能与用户名相同。")
    if normalized.lower() in COMMON_PASSWORDS:
        raise ValueError("新密码过于常见，请换一个更安全的密码。")
