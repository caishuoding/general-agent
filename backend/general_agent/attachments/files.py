"""封装 DeepSeek 图片文件上传和删除，复用当前聊天连接并固定 30 天 TTL。"""

import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from general_agent.infrastructure.settings import SettingsStore, StoredSettings

FILE_TTL_SECONDS = 30 * 24 * 60 * 60


class RemoteImage(BaseModel):
    """校验文件身份及明确过期时间，防止将无 TTL 的上传当作可用。"""

    id: str = Field(pattern=r'^file-api-[A-Za-z0-9_-]+$')
    created_at: int = Field(strict=True, ge=0)
    expires_at: int = Field(strict=True, gt=0)


def file_api_base(settings: StoredSettings) -> str:
    """统一聊天根地址与 /v1 地址，按官方 Files 路径定位文件接口。"""
    base = settings.base_url.rstrip('/')
    return base[:-3] if base.endswith('/v1') else base


class DeepSeekFiles:
    """通过当前 LLM 凭据操作远端图片，不保存或回传额外密钥。"""

    def __init__(self, settings: SettingsStore, transport: httpx.BaseTransport | None = None) -> None:
        """注入聊天设置与 HTTP 传输，测试可替换外部文件接口。"""
        self.settings, self.transport = settings, transport

    def upload(self, path: Path, filename: str, mime: str) -> dict[str, Any]:
        """上传一次经过校验的原图，要求服务端返回不超过 30 天的有效期。"""
        settings = self.settings.read()
        if settings is None:
            raise ValueError('请先配置 LLM 连接，图片上传需要复用其 API 密钥。')
        base = file_api_base(settings)
        try:
            with httpx.Client(transport=self.transport, timeout=httpx.Timeout(600, connect=10), trust_env=False, follow_redirects=False) as client:
                with path.open('rb') as image:
                    response = client.post(
                        base + '/files',
                        headers={'Authorization': 'Bearer ' + settings.api_key},
                        data={'purpose': 'user_data', 'expires_after[anchor]': 'created_at',
                              'expires_after[seconds]': str(FILE_TTL_SECONDS)},
                        files={'file': (filename, image, mime)},
                    )
        except httpx.RequestError:
            raise ValueError('DeepSeek 图片上传连接失败或超时，请重新上传。') from None
        if response.status_code != 200:
            raise ValueError(f'DeepSeek 图片上传失败（HTTP {response.status_code}），请检查 LLM 连接后重新上传。')
        try:
            remote = RemoteImage.model_validate(response.json())
        except ValueError:
            raise ValueError('DeepSeek 未返回有效的文件 ID 和过期时间，图片上传未完成。') from None
        if remote.expires_at <= int(time.time()) or not 0 < remote.expires_at - remote.created_at <= FILE_TTL_SECONDS:
            # 按服务端创建时间核对有效期，避免客户端时钟偏差影响 30 天 TTL。
            raise ValueError('DeepSeek 返回的图片有效期不符合 30 天 TTL，图片上传未完成。')
        return {**remote.model_dump(), 'base_url': base}

    def delete(self, remote: dict[str, Any]) -> None:
        """在来源删除时删除远端图片；已过期或已不存在的文件视为清理完成。"""
        if remote['expires_at'] <= time.time():
            return
        settings = self.settings.read()
        if settings is None or file_api_base(settings) != remote['base_url']:
            raise ValueError('清理远端图片需要原 DeepSeek 服务的 LLM 连接，请恢复连接后重试。')
        try:
            with httpx.Client(transport=self.transport, timeout=30, trust_env=False, follow_redirects=False) as client:
                response = client.delete(
                    remote['base_url'] + '/files/' + remote['id'],
                    headers={'Authorization': 'Bearer ' + settings.api_key},
                )
        except httpx.RequestError:
            raise ValueError('DeepSeek 图片删除连接失败，请重试来源清理。') from None
        if response.status_code not in (200, 404):
            raise ValueError(f'DeepSeek 图片删除失败（HTTP {response.status_code}），请重试来源清理。')
