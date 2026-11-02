"""定义一次 Agent Run 的不可变运行上下文。"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from general_agent.chat.progress import MessageProgress
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.settings import StoredSettings


class AgentRole(StrEnum):
    """区分主智能体与子智能体的业务身份。"""

    MAIN = 'main'
    SUBAGENT = 'subagent'


@dataclass(frozen=True)
class AgentContext:
    """保存一次 Agent Run 的固定身份、配置和传输回调。"""

    role: AgentRole
    conversation_id: UUID
    account_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    run_id: UUID
    settings: StoredSettings
    operation: OperationContext
    delivery: Literal['sync', 'stream']
    context_fragments: tuple[str, ...] = ()
    on_content: Callable[[str], None] | None = None
    on_progress: Callable[[MessageProgress], None] | None = None
    on_round_start: Callable[[], None] | None = None

    @property
    def is_main(self) -> bool:
        """返回当前 Run 是否为主智能体，供节点选择业务生命周期。"""
        return self.role is AgentRole.MAIN

    @property
    def thread_id(self) -> str:
        """返回 LangGraph 稳定线程标识，主回答复用会话线程。"""
        return str(self.conversation_id) if self.is_main else f'run:{self.run_id}'
