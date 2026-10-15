"""OpenAI 兼容文字模型的外部调用边界。"""

import json
from collections.abc import Iterator

import httpx
from pydantic import BaseModel, Field, StrictInt

from general_agent.settings import StoredSettings


class TokenUsage(BaseModel):
    prompt_tokens: StrictInt | None = Field(default=None, ge=0)
    completion_tokens: StrictInt | None = Field(default=None, ge=0)
    total_tokens: StrictInt | None = Field(default=None, ge=0)


class Completion(BaseModel):
    content: str = Field(min_length=1)
    usage: TokenUsage


class StreamChunk(BaseModel):
    content: str = ""
    usage: TokenUsage | None = None


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


    def stream(self, settings: StoredSettings, messages: list[dict[str, str]]) -> Iterator[StreamChunk]:
        """逐段解析 SSE 并保留结束原因之后的用量；断流或提前结束不标记为完整。"""
        usage = TokenUsage()
        has_content = False
        finished = False
        incomplete = False
        try:
            with httpx.Client(transport=self.transport, timeout=60, follow_redirects=False, trust_env=False) as client:
                with client.stream(
                    "POST", settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.api_key},
                    json={"model": settings.model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}},
                ) as response:
                    if not response.is_success:
                        response.read()
                        try:
                            usage = TokenUsage.model_validate(response.json().get("usage") or {})
                        except (ValueError, AttributeError):
                            pass
                        if response.status_code in (401, 403):
                            raise ModelFailure("模型认证失败，请在设置中检查密钥与访问权限。", usage)
                        raise ModelFailure(f"模型请求失败（HTTP {response.status_code}），请检查配置或重试。", usage)
                    data: list[str] = []
                    for line in response.iter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                            continue
                        if line or not data:
                            continue
                        raw = "\n".join(data)
                        data = []
                        if raw == "[DONE]":
                            if incomplete:
                                raise ModelFailure("模型提前结束回答，内容可能不完整，请重试或调整请求。", usage)
                            if not has_content or not finished:
                                raise ModelFailure("模型未返回完整文字回答，请检查模型能力或重试。", usage)
                            return
                        payload = json.loads(raw)
                        if not isinstance(payload, dict):
                            raise ModelFailure("模型流式回答失败，请稍后重试。", usage)
                        if payload.get("usage") is not None:
                            usage = TokenUsage.model_validate(payload["usage"])
                            yield StreamChunk(usage=usage)
                        if payload.get("error"):
                            raise ModelFailure("模型流式回答失败，请稍后重试。", usage)
                        for choice in payload.get("choices", []):
                            if choice.get("index", 0) != 0:
                                continue
                            content = choice.get("delta", {}).get("content")
                            if content is not None and not isinstance(content, str):
                                raise ValueError("无效文字片段")
                            if content:
                                has_content = True
                                yield StreamChunk(content=content)
                            reason = choice.get("finish_reason")
                            if reason == "stop":
                                finished = True
                            elif reason is not None:
                                incomplete = True
        except httpx.TimeoutException:
            raise ModelFailure("模型流式响应超时，已保留部分内容，可以重试。", usage) from None
        except httpx.RequestError:
            raise ModelFailure("模型连接中断，已保留部分内容，可以重试。", usage) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ModelFailure("模型流式响应格式无效，已保留部分内容，可以重试。", usage) from None
        raise ModelFailure("模型回答流中断，已保留部分内容，可以重试。", usage)
