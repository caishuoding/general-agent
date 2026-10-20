"""以受限权限在本机保存模型连接配置，公开接口不返回密钥。"""

import os
import tempfile
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, field_validator


class SettingsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = SecretStr("")
    context_window_tokens: StrictInt = Field(default=32768, ge=1024, le=10_000_000)
    output_reserve_tokens: StrictInt = Field(default=4096, ge=1, le=1_000_000)

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
    context_window_tokens: int = 32768
    output_reserve_tokens: int = 4096


class StoredSettings(BaseModel):
    base_url: str
    model: str
    api_key: str = Field(repr=False)
    revision: str = "legacy"
    context_window_tokens: int = Field(default=32768, ge=1024)
    output_reserve_tokens: int = Field(default=4096, ge=1)

    def public(self) -> PublicSettings:
        """返回连接、读取预算及凭据是否配置，不泄露凭据正文。"""
        return PublicSettings(base_url=self.base_url, model=self.model, api_key_configured=bool(self.api_key),
                              context_window_tokens=self.context_window_tokens, output_reserve_tokens=self.output_reserve_tokens)


class SettingsStore:
    """串行、原子地读写本机模型配置。"""

    def __init__(self, directory: Path, filename: str = "model.json") -> None:
        """指定私有配置文件，创建动作延迟到首次保存。"""
        self.directory = directory
        self.path = directory / filename
        self.lock = RLock()

    def read(self) -> StoredSettings | None:
        """读取完整配置供后端调用使用，没有配置时返回空值。"""
        with self.lock:
            if not self.path.exists():
                return None
            return StoredSettings.model_validate_json(self.path.read_text())

    def save(self, body: SettingsInput) -> PublicSettings:
        """原子保存连接和上下文预算，实际变更生成版本，同地址空密钥保留原凭据。"""
        with self.lock:
            previous = self.read()
            key = body.api_key.get_secret_value().strip()
            if not key and previous is not None and previous.base_url == body.base_url:
                key = previous.api_key
            if not key:
                raise ValueError("请填写此服务的 API 密钥。")
            if body.output_reserve_tokens >= body.context_window_tokens:
                raise ValueError('回答预留必须小于模型上下文窗口。')
            settings = StoredSettings(base_url=body.base_url, model=body.model, api_key=key,
                                      context_window_tokens=body.context_window_tokens, output_reserve_tokens=body.output_reserve_tokens)
            if previous and previous.model_dump(exclude={'revision'}) == settings.model_dump(exclude={'revision'}):
                settings.revision = previous.revision
            else:
                settings.revision = str(uuid4())
            self.write(settings)
            return settings.public()

    def write(self, settings: StoredSettings) -> None:
        """以原子替换写入受限权限文件，避免读取半份配置。"""
        with self.lock:
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


class EmbeddingSettingsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    base_url: str = ""
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = SecretStr("")
    dimensions: StrictInt = Field(ge=1, le=65536)
    reuse_chat_connection: bool = False


class PublicEmbeddingSettings(PublicSettings):
    dimensions: int | None = None


class StoredEmbeddingSettings(StoredSettings):
    dimensions: int = Field(ge=1, le=65536)

    def public(self) -> PublicEmbeddingSettings:
        """展示嵌入模型及预期维度，凭据只返回配置状态。"""
        return PublicEmbeddingSettings(base_url=self.base_url, model=self.model, api_key_configured=bool(self.api_key), dimensions=self.dimensions)


class EmbeddingSettingsStore(SettingsStore):
    """独立保存嵌入连接，明确复用操作不会把聊天模型当成嵌入模型。"""

    def __init__(self, directory: Path) -> None:
        """为嵌入连接选择独立的私有配置文件。"""
        super().__init__(directory, "embedding.json")

    def read(self) -> StoredEmbeddingSettings | None:
        """读取嵌入连接与预期维度，没有配置时返回空值。"""
        with self.lock:
            if not self.path.exists():
                return None
            return StoredEmbeddingSettings.model_validate_json(self.path.read_text())

    def prepare(self, body: EmbeddingSettingsInput, chat: StoredSettings | None) -> StoredEmbeddingSettings:
        """解析独立或显式复用的连接，保留同地址旧密钥并为实际变更生成新版本。"""
        previous = self.read()
        if body.reuse_chat_connection:
            if chat is None:
                raise ValueError("请先保存聊天连接，再复用它的地址与密钥。")
            base_url, key = chat.base_url, chat.api_key
        else:
            validated = SettingsInput(base_url=body.base_url, model=body.model, api_key=body.api_key)
            base_url, key = validated.base_url, validated.api_key.get_secret_value().strip()
            if not key and previous and previous.base_url == base_url:
                key = previous.api_key
        if not key:
            raise ValueError("请填写嵌入服务的 API 密钥。")
        saved = StoredEmbeddingSettings(base_url=base_url, model=body.model, api_key=key, dimensions=body.dimensions)
        if previous and previous.model_dump(exclude={"revision"}) == saved.model_dump(exclude={"revision"}):
            saved.revision = previous.revision
        else:
            saved.revision = str(uuid4())
        return saved
