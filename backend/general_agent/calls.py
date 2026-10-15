"""所有已接入模型调用共用的显式上下文网关，供 Mem0 和后续工具复用。"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal
from uuid import UUID

from general_agent.budget import BudgetService, OperationContext, Unit
from general_agent.provider import Completion, Embedding, ModelClient, ModelMessage, ModelFailure, StreamChunk, TokenUsage
from general_agent.settings import StoredEmbeddingSettings, StoredSettings


class CallGateway:
    """将原始模型协议封装在预算与计量边界内。"""

    def __init__(self, budget: BudgetService, provider: ModelClient) -> None:
        """绑定持久化预算和原始传输，调用者不需要接触原始 HTTP 客户端。"""
        self.budget, self.provider = budget, provider

    def bind(self, context: OperationContext) -> "BoundCalls":
        """返回操作专属适配器；SDK 线程池不会丢失或混用此显式上下文。"""
        return BoundCalls(self, context)


class BoundCalls:
    """提供绑定同一操作的聊天、能力检查及嵌入，每次 outbound 单独检查。"""

    def __init__(self, gateway: CallGateway, context: OperationContext) -> None:
        """保存不可变操作身份，禁止通过线程局部变量隐式选择操作。"""
        self.gateway, self.context = gateway, context

    @contextmanager
    def call(self, settings: StoredSettings, unit: Unit, reserved: UUID | None = None, assistant_message_id: UUID | None = None) -> Iterator[UUID]:
        """包围一次真实调用并持久化失败用量；业务解析和 SDK 写入失败不会回滚计量。"""
        budget = self.gateway.budget
        call_id = reserved or budget.reserve(self.context, settings, unit)
        budget.begin(self.context, call_id, settings, assistant_message_id)
        try:
            yield call_id
        except ModelFailure as error:
            budget.record(call_id, error.usage, "failed")
            raise
        except BaseException:
            budget.record(call_id, TokenUsage(), "failed")
            raise
        else:
            budget.record(call_id, TokenUsage(), "complete")

    def complete(self, settings: StoredSettings, messages: list[ModelMessage], reserved: UUID | None = None, assistant_message_id: UUID | None = None) -> Completion:
        """复用可选预留并关联回答尝试，检查预算及记录同步文字、视觉或分段请求的实际用量。"""
        with self.call(settings, "chat_tokens", reserved, assistant_message_id) as call_id:
            result = self.gateway.provider.complete(settings, messages)
            self.gateway.budget.record(call_id, result.usage)
            return result

    def probe(self, settings: StoredSettings, capability: Literal["text", "image", "tools"]) -> TokenUsage:
        """使文字、图片和工具能力探针遵守与聊天相同的预算边界。"""
        with self.call(settings, "chat_tokens") as call_id:
            usage = self.gateway.provider.probe(settings, capability)
            self.gateway.budget.record(call_id, usage)
            return usage

    def embed(self, settings: StoredEmbeddingSettings, texts: list[str], reserved: UUID | None = None) -> Embedding:
        """复用可选首步预留，按实际批量嵌入记录输入用量，不要求不存在的输出用量。"""
        with self.call(settings, "embedding_tokens", reserved) as call_id:
            result = self.gateway.provider.embed(settings, texts)
            self.gateway.budget.record(call_id, result.usage)
            return result

    def stream(self, settings: StoredSettings, messages: list[ModelMessage], reserved: UUID, assistant_message_id: UUID) -> Iterator[StreamChunk]:
        """消费已预留的文字或视觉流式调用，先记录用量尾帧再交给界面更新。"""
        with self.call(settings, "chat_tokens", reserved, assistant_message_id) as call_id:
            for chunk in self.gateway.provider.stream(settings, messages):
                if chunk.usage is not None:
                    self.gateway.budget.record(call_id, chunk.usage)
                yield chunk
