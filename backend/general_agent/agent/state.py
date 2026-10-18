"""定义工具决策图在节点之间传递的最小状态。"""

from typing import Any, TypedDict


class AgentState(TypedDict):
    """保存模型消息、待执行调用和本轮工具循环结果。"""

    messages: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    round_index: int
    used_tools: bool
    answer: str | None
    handled: bool
