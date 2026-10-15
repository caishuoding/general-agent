"""OpenAI 兼容文字模型的外部调用边界。"""

import json
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, Field, StrictInt, FiniteFloat

from general_agent.settings import StoredSettings, StoredEmbeddingSettings


class TokenUsage(BaseModel):
    prompt_tokens: StrictInt | None = Field(default=None, ge=0)
    completion_tokens: StrictInt | None = Field(default=None, ge=0)
    total_tokens: StrictInt | None = Field(default=None, ge=0)


RED_IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAF0lEQVR4nGP4z8BAEiJN9aiGUQ1DSgMAkPn/Afnh+ngAAAAASUVORK5CYII="


class EmbeddingVector(BaseModel):
    index: StrictInt = Field(ge=0)
    embedding: list[Annotated[FiniteFloat, Field(strict=True)]] = Field(min_length=1)


class Embedding(BaseModel):
    model: str = Field(min_length=1)
    data: list[EmbeddingVector] = Field(min_length=1)
    usage: TokenUsage

    @property
    def dimensions(self) -> int:
        """返回已验证的向量维度，供后续检索索引使用。"""
        return len(self.data[0].embedding)


class Completion(BaseModel):
    content: str = Field(min_length=1)
    usage: TokenUsage


class ModelFailure(Exception):
    """携带可公开的错误说明及已取得的用量，不保留原始服务错误正文。"""

    def __init__(self, message: str, usage: TokenUsage | None = None) -> None:
        """保存经过应用定义的安全消息，缺失用量保持未知。"""
        super().__init__(message)
        self.usage = usage or TokenUsage()


class ModelClient:
    """通过可替换的 HTTP 传输调用已配置模型。"""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        """允许测试只替换外部网络响应，不替换应用内部协作对象。"""
        self.transport = transport

    def complete(self, settings: StoredSettings, messages: list[dict[str, str]]) -> Completion:
        """请求文字回答，隔离服务错误正文，并保留成功或失败响应中的已知用量。"""
        try:
            with httpx.Client(transport=self.transport, timeout=60, follow_redirects=False, trust_env=False) as client:
                response = client.post(
                    settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.api_key},
                    json={"model": settings.model, "messages": messages, "stream": False},
                )
        except httpx.TimeoutException:
            raise ModelFailure("模型响应超时，请检查连接后重新发送。") from None
        except httpx.RequestError:
            raise ModelFailure("无法连接模型服务，请检查 API 地址与网络。") from None
        payload = None
        usage = TokenUsage()
        try:
            payload = response.json()
            usage = TokenUsage.model_validate(payload.get("usage") or {})
        except (ValueError, AttributeError):
            pass
        if response.status_code in (401, 403):
            raise ModelFailure("模型认证失败，请在设置中检查密钥与访问权限。", usage)
        if not response.is_success:
            raise ModelFailure(f"模型请求失败（HTTP {response.status_code}），请检查模型配置或稍后重新发送。", usage)
        try:
            if not isinstance(payload, dict):
                raise ValueError("响应不是 JSON 对象。")
            return Completion(content=payload["choices"][0]["message"]["content"], usage=usage)
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelFailure("模型未返回有效文字回答，请检查模型是否支持此接口。", usage) from None

    def request(self, settings: StoredSettings, endpoint: str, body: dict[str, object]) -> tuple[dict[str, object], TokenUsage]:
        """发送非流式协议请求，屏蔽原始错误正文并携带可取得的用量。"""
        try:
            with httpx.Client(transport=self.transport, timeout=60, follow_redirects=False, trust_env=False) as client:
                response = client.post(settings.base_url + endpoint, headers={"Authorization": "Bearer " + settings.api_key}, json={"model": settings.model, **body})
        except httpx.TimeoutException:
            raise ModelFailure("模型响应超时，请检查连接后重新验证。") from None
        except httpx.RequestError:
            raise ModelFailure("无法连接模型服务，请检查 API 地址与网络。") from None
        usage = TokenUsage()
        payload = None
        try:
            payload = response.json()
            usage = TokenUsage.model_validate(payload.get("usage") or {})
        except (ValueError, AttributeError):
            pass
        if response.status_code in (401, 403):
            raise ModelFailure("模型认证失败，请在设置中检查密钥与访问权限。", usage)
        if not response.is_success:
            raise ModelFailure(f"能力请求失败（HTTP {response.status_code}），服务可能不支持该能力或当前参数。", usage)
        if not isinstance(payload, dict):
            raise ModelFailure("服务未返回有效 JSON 对象，请检查接口兼容性。", usage)
        return payload, usage

    def probe(self, settings: StoredSettings, capability: Literal["text", "image", "tools"]) -> TokenUsage:
        """分别发送文字、图片或工具请求，验证文字内容及工具调用标识和参数。"""
        body: dict[str, object] = {"stream": False}
        if capability == "image":
            body["messages"] = [{"role": "user", "content": [
                {"type": "text", "text": "Name the dominant color in this image in one English word."},
                {"type": "image_url", "image_url": {"url": RED_IMAGE, "detail": "low"}},
            ]}]
        else:
            body["messages"] = [{"role": "user", "content": "Reply OK." if capability == "text" else "Call capability_check with value OK."}]
        if capability == "tools":
            body["tools"] = [{"type": "function", "function": {"name": "capability_check", "description": "Report the capability check value.", "parameters": {"type": "object", "properties": {"value": {"type": "string", "enum": ["OK"]}}, "required": ["value"], "additionalProperties": False}}}]
            body["tool_choice"] = {"type": "function", "function": {"name": "capability_check"}}
        payload, usage = self.request(settings, "/chat/completions", body)
        try:
            message = payload["choices"][0]["message"]  # type: ignore[index]
            if capability == "tools":
                call = message["tool_calls"][0]
                if not isinstance(call["id"], str) or not call["id"].strip() or call["type"] != "function" or call["function"]["name"] != "capability_check" or json.loads(call["function"]["arguments"]) != {"value": "OK"}:
                    raise ValueError("工具响应无效")
            else:
                content = message["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("文字为空")
                if capability == "image" and content.strip().rstrip(".。!").upper() != "RED":
                    raise ValueError("未识别图片")
        except (ValueError, KeyError, IndexError, TypeError):
            label = {"text": "文字回答", "image": "图片识别", "tools": "工具调用"}[capability]
            raise ModelFailure(f"{label}验证未通过：服务响应缺少预期内容或格式；请核对该模型是否支持此能力。", usage) from None
        return usage

    def embed(self, settings: StoredEmbeddingSettings, texts: list[str]) -> Embedding:
        """调用独立嵌入接口并严格校验模型标识、条数、有限数值及预期维度。"""
        payload, usage = self.request(settings, "/embeddings", {"input": texts, "encoding_format": "float"})
        try:
            result = Embedding.model_validate({"model": payload.get("model"), "data": payload.get("data"), "usage": usage})
            if result.model != settings.model:
                raise ValueError("模型标识不同")
            if len(result.data) != len(texts) or sorted(item.index for item in result.data) != list(range(len(texts))):
                raise ValueError("嵌入条数或索引不符")
            if any(len(item.embedding) != settings.dimensions for item in result.data):
                raise ValueError("嵌入维度不符")
        except (ValueError, TypeError):
            raise ModelFailure(f"嵌入验证失败：响应必须匹配模型 {settings.model}、预期维度 {settings.dimensions}，且向量必须为有限数值。", usage) from None
        return result
