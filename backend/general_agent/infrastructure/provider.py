"""OpenAI 兼容文字、视觉与嵌入模型的外部调用边界。"""

import json
from collections.abc import Iterator
from typing import Annotated, Any, Literal, TypedDict

import httpx
from langsmith import traceable
from pydantic import BaseModel, Field, StrictInt, FiniteFloat

from general_agent.infrastructure.settings import StoredSettings, StoredEmbeddingSettings
from general_agent.infrastructure.tracing import model_inputs, model_outputs, stream_outputs


class TextPart(TypedDict):
    """为多模态请求提供文字描述及受信任来源标识。"""
    type: Literal['text']
    text: str


class FilePart(TypedDict):
    """通过 DeepSeek Files API 返回的身份引用图片，不重复发送原始字节。"""
    type: Literal['file']
    file_id: str


class ModelMessage(TypedDict):
    """使用明确的文字或多模态内容类型描述模型消息。"""
    role: str
    content: str | list[TextPart | FilePart]


class TokenUsage(BaseModel):
    prompt_tokens: StrictInt | None = Field(default=None, ge=0)
    completion_tokens: StrictInt | None = Field(default=None, ge=0)
    total_tokens: StrictInt | None = Field(default=None, ge=0)


    @classmethod
    def from_payload(cls, payload: object) -> "TokenUsage":
        """逐字段保留非负整数用量，一个缺失或畸形字段不抹掉其余真实数值。"""
        data = payload if isinstance(payload, dict) else {}
        return cls.model_validate({key: value for key, value in data.items() if key in cls.model_fields and type(value) is int and 0 <= value <= 2147483647})


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


class ToolReply(BaseModel):
    """保留标准工具回合及推理字段，下一回合按服务端原样回传。"""
    message: dict[str, Any]
    usage: TokenUsage


class ToolCallDelta(BaseModel):
    """保存流式工具调用的单个分片，调用方按 index 累积完整参数。"""
    index: StrictInt = Field(default=0, ge=0)
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


class StreamChunk(BaseModel):
    content: str = ""
    reasoning_content: str = ""
    tool_call: ToolCallDelta | None = None
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

    @traceable(name="chat.complete", run_type="llm", process_inputs=model_inputs, process_outputs=model_outputs)
    def complete(self, settings: StoredSettings, messages: list[ModelMessage]) -> Completion:
        """追踪文字或视觉回答，排除连接密钥并保留安全错误与真实用量。"""
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
            usage = TokenUsage.from_payload(payload.get("usage"))
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


    @traceable(name="chat.stream", run_type="llm", process_inputs=model_inputs, reduce_fn=stream_outputs)
    def stream(self, settings: StoredSettings, messages: list[ModelMessage], tools: list[dict[str, Any]] | None = None) -> Iterator[StreamChunk]:
        """逐段解析并追踪文字、推理和工具调用 SSE，密钥排除且用量按真实尾帧汇总。"""
        usage = TokenUsage()
        has_content = False
        has_tool_calls = False
        finished = False
        incomplete = False
        body: dict[str, object] = {"model": settings.model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
        if tools is not None:
            body.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        try:
            with httpx.Client(transport=self.transport, timeout=60, follow_redirects=False, trust_env=False) as client:
                with client.stream(
                    "POST", settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.api_key},
                    json=body,
                ) as response:
                    if not response.is_success:
                        response.read()
                        try:
                            usage = TokenUsage.from_payload(response.json().get("usage"))
                        except (ValueError, AttributeError):
                            pass
                        if response.status_code in (401, 403):
                            raise ModelFailure("模型认证失败，请在设置中检查密钥与访问权限。", usage)
                        raise ModelFailure(f"模型请求失败（HTTP {response.status_code}），请检查配置或重试。", usage)
                    data: list[str] = []
                    raw_lines: list[str] = []
                    saw_data = False
                    for line in response.iter_lines():
                        if line.startswith("data:"):
                            saw_data = True
                            data.append(line[5:].lstrip())
                            continue
                        if line:
                            raw_lines.append(line)
                        if line or not data:
                            continue
                        raw = "\n".join(data)
                        data = []
                        if raw == "[DONE]":
                            if incomplete:
                                raise ModelFailure("模型提前结束回答，内容可能不完整，请重试或调整请求。", usage)
                            if not finished or (not has_content and not has_tool_calls):
                                raise ModelFailure("模型未返回完整文字回答，请检查模型能力或重试。", usage)
                            return
                        payload = json.loads(raw)
                        if not isinstance(payload, dict):
                            raise ModelFailure("模型流式回答失败，请稍后重试。", usage)
                        if payload.get("usage") is not None:
                            usage = TokenUsage.from_payload(payload["usage"])
                            yield StreamChunk(usage=usage)
                        if payload.get("error"):
                            raise ModelFailure("模型流式回答失败，请稍后重试。", usage)
                        for choice in payload.get("choices", []):
                            if choice.get("index", 0) != 0:
                                continue
                            delta = choice.get("delta", {})
                            content = delta.get("content")
                            if content is not None and not isinstance(content, str):
                                raise ValueError("无效文字片段")
                            if content:
                                has_content = True
                                yield StreamChunk(content=content)
                            reasoning = delta.get("reasoning_content")
                            if reasoning is not None and not isinstance(reasoning, str):
                                raise ValueError("无效推理片段")
                            if reasoning:
                                yield StreamChunk(reasoning_content=reasoning)
                            tool_deltas = delta.get("tool_calls") or []
                            if not isinstance(tool_deltas, list):
                                raise ValueError("无效工具调用片段")
                            for item in tool_deltas:
                                if not isinstance(item, dict):
                                    raise ValueError("无效工具调用片段")
                                function = item.get("function") or {}
                                if not isinstance(function, dict):
                                    raise ValueError("无效工具函数片段")
                                has_tool_calls = True
                                yield StreamChunk(tool_call=ToolCallDelta(
                                    index=item.get("index", 0) if type(item.get("index", 0)) is int else 0,
                                    id=item.get("id") if isinstance(item.get("id"), str) else None,
                                    name=function.get("name") if isinstance(function.get("name"), str) else None,
                                    arguments=function.get("arguments") if isinstance(function.get("arguments"), str) else None,
                                ))
                            reason = choice.get("finish_reason")
                            if reason in ("stop", "tool_calls"):
                                finished = True
                            elif reason is not None:
                                incomplete = True
        except httpx.TimeoutException:
            raise ModelFailure("模型流式响应超时，已保留部分内容，可以重试。", usage) from None
        except httpx.RequestError:
            raise ModelFailure("模型连接中断，已保留部分内容，可以重试。", usage) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ModelFailure("模型流式响应格式无效，已保留部分内容，可以重试。", usage) from None
        if raw_lines and not saw_data:
            try:
                payload = json.loads('\n'.join(raw_lines))
                usage = TokenUsage.from_payload(payload.get('usage'))
            except (ValueError, AttributeError):
                pass
            raise ModelFailure("模型未返回有效流式回答，请检查模型是否支持流式工具调用。", usage)
        raise ModelFailure("模型回答流中断，已保留部分内容，可以重试。", usage)

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
            usage = TokenUsage.from_payload(payload.get("usage"))
        except (ValueError, AttributeError):
            pass
        if response.status_code in (401, 403):
            raise ModelFailure("模型认证失败，请在设置中检查密钥与访问权限。", usage)
        if not response.is_success:
            raise ModelFailure(f"能力请求失败（HTTP {response.status_code}），服务可能不支持该能力或当前参数。", usage)
        if not isinstance(payload, dict):
            raise ModelFailure("服务未返回有效 JSON 对象，请检查接口兼容性。", usage)
        return payload, usage

    @traceable(name="chat.structured", run_type="llm", process_inputs=model_inputs, process_outputs=model_outputs)
    def structured(self, settings: StoredSettings, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, json_mode: bool = False) -> ToolReply:
        """追踪 JSON 或函数调用回合，排除密钥、验证协议并保留推理续传字段。"""
        body: dict[str, object] = {"messages": messages, "stream": False}
        if tools is not None:
            body.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        payload, usage = self.request(settings, "/chat/completions", body)
        try:
            message = payload["choices"][0]["message"]  # type: ignore[index]
            if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
                raise ValueError("无效角色")
            content = message.get("content")
            calls = message.get("tool_calls", [])
            if content is not None and not isinstance(content, str):
                raise ValueError("无效正文")
            if not isinstance(calls, list) or len(calls) > 8 or (not content and not calls):
                raise ValueError("空响应或工具过多")
            identifiers: set[str] = set()
            for call in calls:
                if not isinstance(call.get("id"), str) or not call["id"] or call["id"] in identifiers or call.get("type") != "function":
                    raise ValueError("无效工具身份")
                identifiers.add(call["id"])
                function = call["function"]
                if not isinstance(function["name"], str) or not isinstance(function["arguments"], str) or not isinstance(json.loads(function["arguments"]), dict):
                    raise ValueError("无效工具参数")
            result = {key: value for key, value in message.items() if key in ("content", "tool_calls", "reasoning_content")}
            result["role"] = "assistant"
            return ToolReply(message=result, usage=usage)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelFailure("模型未返回有效的记忆或工具响应，请检查模型能力后重试。", usage) from None

    @traceable(name="model.embed", run_type="embedding", process_inputs=model_inputs, process_outputs=model_outputs)
    def embed(self, settings: StoredEmbeddingSettings, texts: list[str]) -> Embedding:
        """追踪独立嵌入并排除密钥，严格校验模型标识、条数、有限数值及预期维度。"""
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
