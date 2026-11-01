"""定义对话和智能体循环共享的可恢复工作状态。"""

from typing import Annotated, Any, NotRequired, TypedDict


def merge_messages(
    current: list[dict[str, Any]], update: list[dict[str, Any]] | dict[str, Any],
) -> list[dict[str, Any]]:
    """合并工作消息；节点可追加消息或显式替换压缩后的完整快照。"""
    if isinstance(update, dict) and isinstance(update.get('replace'), list):
        return [dict(message) for message in update['replace']]
    messages = update if isinstance(update, list) else [update]
    return [*current, *(dict(message) for message in messages)]


class AgentState(TypedDict):
    """保存跨轮工作消息及当前回答模型、工具和收尾控制状态。"""

    version: NotRequired[int]
    messages: Annotated[list[dict[str, Any]], merge_messages]
    used_skills: list[str]
    tool_calls: list[dict[str, Any]]
    round_index: int
    tool_failures: dict[str, int]
    document_calls: dict[str, int]
    used_tools: bool
    answer: str | None
    inbox_deliveries: list[str]
    settled: bool
    watching_inbox: bool
    answer_id: NotRequired[str]
    root_run_id: NotRequired[str]
    source_snapshot: NotRequired[dict[str, Any]]
    source_attachment_ids: NotRequired[list[str]]
    failure: NotRequired[str | None]
    knowledge_source_candidates: NotRequired[list[dict[str, Any]]]
    web_sources: NotRequired[list[dict[str, Any]]]
    knowledge_sources: NotRequired[list[dict[str, Any]]]
