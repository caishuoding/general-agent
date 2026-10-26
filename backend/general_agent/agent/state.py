"""定义工具决策图在节点之间传递的最小状态。"""

from typing import Any, TypedDict


class AgentState(TypedDict):
    """保存模型消息、待执行调用、工具失败次数和文档工具调用次数及最终结果。"""

    messages: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    round_index: int
    tool_failures: dict[str, int]
    document_calls: dict[str, int]
    used_tools: bool
    answer: str | None
    handled: bool
    inbox_deliveries: list[str]
    settled: bool
