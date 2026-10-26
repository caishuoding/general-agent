"""所有已接入模型调用共用的显式上下文网关，供 Mem0 和后续工具复用。"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from general_agent.infrastructure.operations import OperationContext, OperationService, Unit
from general_agent.infrastructure.provider import Completion, Embedding, ModelClient, ModelMessage, ModelFailure, StreamChunk, TokenUsage, ToolReply
from general_agent.infrastructure.settings import StoredEmbeddingSettings, StoredSettings


class CallGateway:
    """将原始模型协议封装在操作身份与用量记录边界内。"""

    def __init__(self, operations: OperationService, provider: ModelClient) -> None:
        """绑定持久化操作账本和原始传输，调用者不需要接触原始 HTTP 客户端。"""
        self.operations, self.provider = operations, provider

    def bind(self, context: OperationContext) -> "BoundCalls":
        """返回操作专属适配器；SDK 线程池不会丢失或混用此显式上下文。"""
        return BoundCalls(self, context)


class BoundCalls:
    """提供绑定同一操作的聊天及嵌入调用，每次 outbound 单独检查。"""

    def __init__(self, gateway: CallGateway, context: OperationContext) -> None:
        """保存不可变操作身份，禁止通过线程局部变量隐式选择操作。"""
        self.gateway, self.context = gateway, context

    @contextmanager
    def call(self, settings: StoredSettings, unit: Unit, reserved: UUID | None = None, assistant_message_id: UUID | None = None) -> Iterator[UUID]:
        """记录真实调用与失败用量；外发许可被拒绝时终结预留，避免留下永久等待记录。"""
        operations = self.gateway.operations
        call_id = reserved or operations.reserve(self.context, settings, unit)
        try:
            operations.begin(self.context, call_id, settings, assistant_message_id)
        except ValueError:
            operations.cancel_reservation(call_id)
            raise
        try:
            yield call_id
        except ModelFailure as error:
            operations.record(call_id, error.usage, "failed")
            raise
        except BaseException:
            operations.record(call_id, TokenUsage(), "failed")
            raise
        else:
            operations.record(call_id, TokenUsage(), "complete")

    def complete(self, settings: StoredSettings, messages: list[ModelMessage], reserved: UUID | None = None, assistant_message_id: UUID | None = None) -> Completion:
        """复用可选预留并关联回答尝试，记录同步文字、视觉或分段请求的实际用量。"""
        with self.call(settings, "chat_tokens", reserved, assistant_message_id) as call_id:
            result = self.gateway.provider.complete(settings, messages)
            self.gateway.operations.record(call_id, result.usage)
            return result

    def structured(self, settings: StoredSettings, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, json_mode: bool = False) -> ToolReply:
        """让 Mem0 推理与智能体工具回合复用聊天配置和真实用量记录。"""
        with self.call(settings, "chat_tokens") as call_id:
            result = self.gateway.provider.structured(settings, messages, tools, json_mode)
            self.gateway.operations.record(call_id, result.usage)
            return result

    def embed(self, settings: StoredEmbeddingSettings, texts: list[str], reserved: UUID | None = None) -> Embedding:
        """复用可选首步预留，按实际批量嵌入记录输入用量，不要求不存在的输出用量。"""
        with self.call(settings, "embedding_tokens", reserved) as call_id:
            result = self.gateway.provider.embed(settings, texts)
            self.gateway.operations.record(call_id, result.usage)
            return result

    def stream(
        self,
        settings: StoredSettings,
        messages: list[ModelMessage],
        reserved: UUID | None = None,
        assistant_message_id: UUID | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[StreamChunk]:
        """消费统一的文字、推理与工具调用流，先记录用量尾帧再交给调用方。"""
        with self.call(settings, "chat_tokens", reserved, assistant_message_id) as call_id:
            for chunk in self.gateway.provider.stream(settings, messages, tools):
                if chunk.usage is not None:
                    self.gateway.operations.record(call_id, chunk.usage)
                yield chunk
