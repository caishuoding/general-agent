"""以受限权限在本机保存模型连接配置，公开接口不返回密钥。"""

import os
import tempfile
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class SettingsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = SecretStr("")

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """允许 HTTPS 或回环 HTTP，禁止 URL 中携带凭据或查询参数。"""
        parts = urlsplit(value)
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("请填写不含凭据和查询参数的 API 地址。")
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("云端服务需要 HTTPS，本机服务可使用 HTTP。")
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError("端口无效。")
        return value.rstrip("/")


class PublicSettings(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key_configured: bool = False


class StoredSettings(BaseModel):
    base_url: str
    model: str
    api_key: str = Field(repr=False)

    def public(self) -> PublicSettings:
        """仅返回连接信息与凭据是否配置，不泄露凭据正文。"""
        return PublicSettings(base_url=self.base_url, model=self.model, api_key_configured=bool(self.api_key))


class SettingsStore:
    """串行、原子地读写本机模型配置。"""

    def __init__(self, directory: Path) -> None:
        """指定私有配置目录，创建动作延迟到首次保存。"""
        self.directory = directory
        self.path = directory / "model.json"
        self.lock = RLock()

    def read(self) -> StoredSettings | None:
        """读取完整配置供后端调用使用，没有配置时返回空值。"""
        with self.lock:
            if not self.path.exists():
                return None
            return StoredSettings.model_validate_json(self.path.read_text())

    def save(self, body: SettingsInput) -> PublicSettings:
        """原子保存配置，同一地址下空密钥保留原凭据。"""
        with self.lock:
            previous = self.read()
            key = body.api_key.get_secret_value().strip()
            if not key and previous is not None and previous.base_url == body.base_url:
                key = previous.api_key
            if not key:
                raise ValueError("请填写此服务的 API 密钥。")
            settings = StoredSettings(base_url=body.base_url, model=body.model, api_key=key)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary = tempfile.mkstemp(dir=self.directory)
            try:
                with os.fdopen(descriptor, "w") as output:
                    output.write(settings.model_dump_json())
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return settings.public()
