"""通过统一计数接口隐藏模型路由、官方分词格式与明确标记的保守估算。"""

import json
import os
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from tokenizers import Tokenizer

from general_agent.infrastructure.settings import StoredSettings


class _Registration(BaseModel):
    """声明一个精确连接与模型对应的本地官方分词数据。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    base_url: str
    model: str
    adapter: Literal['huggingface']
    path: str
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    source: str


class _Registry(BaseModel):
    """校验持久注册文件，禁止配置拼写错误被当成未配置。"""

    model_config = ConfigDict(extra='forbid', strict=True)
    models: list[_Registration]


class _TextCounter(Protocol):
    """约束适配器的最小能力，外层无需了解各模型分词数据格式。"""

    method: str
    identity: bytes

    def count(self, text: str) -> int:
        """返回普通文本计数，不包含模型聊天协议额外加入的 Token。"""
        ...


class _ByteEstimate:
    """没有精确匹配的实现时，明确使用 UTF-8 字节数作为保守估算。"""

    method = 'character_estimate'
    identity = b'utf8-conservative-v1'

    def count(self, text: str) -> int:
        """按字节数估算，避免把中文字符数误称为模型 Token 数。"""
        return len(text.encode('utf-8'))


@lru_cache(maxsize=8)
def _load_tokenizer(data: bytes) -> Tokenizer:
    """按文件内容缓存只读分词器，移除数据文件携带的截断和补齐设置。"""
    tokenizer = Tokenizer.from_str(data.decode('utf-8'))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


class _HuggingFaceCounter:
    """通过通用 tokenizer.json 支持不同服务商提供的官方分词数据。"""

    method = 'official_tokenizer'

    def __init__(self, data: bytes) -> None:
        """固定已经校验过的文件内容，任务期间不受配置文件替换影响。"""
        self.identity = sha256(data).digest()
        self.tokenizer = _load_tokenizer(data)

    def count(self, text: str) -> int:
        """按普通文本编码计数，不自动添加 BOS、EOS 或聊天模板。"""
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)


def _resolve(model: str, base_url: str, directory: Path | None) -> _TextCounter:
    """优先兼容显式环境映射，否则精确匹配本地注册；损坏配置直接报错。"""
    paths = TypeAdapter(dict[str, str]).validate_json(os.environ.get('MODEL_TOKENIZERS', '{}'))
    if model in paths:
        return _HuggingFaceCounter(Path(paths[model]).read_bytes())
    if directory is not None and (directory / 'tokenizers.json').exists():
        registry = _Registry.model_validate_json((directory / 'tokenizers.json').read_bytes())
        matches = [item for item in registry.models if item.model == model and item.base_url.rstrip('/') == base_url]
        if len(matches) > 1:
            raise ValueError('同一个模型连接不能注册多个分词器。')
        if matches:
            item = matches[0]
            data = (directory / item.path).read_bytes()
            if sha256(data).hexdigest() != item.sha256:
                raise ValueError('官方分词器文件校验失败，请重新安装对应模型的分词数据。')
            return _HuggingFaceCounter(data)
    return _ByteEstimate()


class TokenCounter:
    """对外统一计数、预算截取和上下文核算，不要求业务调用方判断模型服务商。"""

    def __init__(self, model: str, *, base_url: str = '', directory: Path | None = None) -> None:
        """按连接与模型解析计数适配器，保留单模型构造方式供既有调用使用。"""
        self.model = model
        self.base_url = base_url.rstrip('/')
        self._adapter = _resolve(model, self.base_url, directory)
        self.method = self._adapter.method
        identity = json.dumps([self.base_url, model, self.method], ensure_ascii=False).encode()
        self.fingerprint = sha256(identity + b'\n' + self._adapter.identity).hexdigest()

    @classmethod
    def for_settings(cls, settings: StoredSettings | None, directory: Path) -> 'TokenCounter':
        """从当前 LLM 连接构造计数快照，外层切换模型时不需要改变调用方式。"""
        return cls(settings.model if settings else 'document-default',
                   base_url=settings.base_url if settings else '', directory=directory)

    def count(self, text: str) -> int:
        """使用本次固定的适配器计数，具体方法可从 method 读取。"""
        return self._adapter.count(text)

    def available(self, settings: StoredSettings, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        """估算消息和工具的文本预算并预留协议空间；整轮精确用量以模型 usage 为准。"""
        serialized = json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False)
        return max(0, settings.context_window_tokens - settings.output_reserve_tokens - self.count(serialized) - 512)

    def prefix(self, text: str, budget: int) -> str:
        """寻找预算内的连续字符前缀；只切字符边界，并核验返回文本的实际计数。"""
        if budget <= 0:
            return ''
        if self.count(text) <= budget:
            return text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.count(text[:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        return text[:low]
