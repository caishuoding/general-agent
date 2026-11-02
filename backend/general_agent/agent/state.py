"""定义唯一 Agent 图的精简、可恢复和可人工检查的运行状态。"""

from typing import Annotated, Any, Literal, TypedDict

STATE_VERSION = 4


class LoadedSkill(TypedDict):
    """描述当前 Agent Run 已加载并允许访问的 Skill。"""

    name: str
    version: str
    root: str
    entry: str


class AttachmentAccess(TypedDict):
    """描述当前 Run 可读取的附件身份，不保存附件正文。"""

    id: str
    filename: str
    kind: Literal['document', 'image', 'source']


class KnowledgeBaseAccess(TypedDict):
    """描述当前会话可检索的知识库身份，不保存切片正文。"""

    id: str
    name: str


def merge_agent_messages(
    current: list[dict[str, Any]], update: list[dict[str, Any]] | dict[str, Any],
) -> list[dict[str, Any]]:
    """合并工作消息；支持完整替换，并按稳定消息 ID 更新或追加。"""
    if isinstance(update, dict) and isinstance(update.get('replace'), list):
        return [dict(message) for message in update['replace']]
    incoming = update if isinstance(update, list) else [update]
    merged = [dict(message) for message in current]
    for message in incoming:
        item = dict(message)
        identifier = str(item.get('id') or '')
        index = next((position for position, existing in enumerate(merged)
                      if identifier and str(existing.get('id') or '') == identifier), None)
        if index is None:
            merged.append(item)
        else:
            merged[index] = item
    return merged


class AgentState(TypedDict):
    """保存唯一 Agent 图中需要恢复、路由和人工检查的动态数据。"""

    version: int
    run_id: str
    messages: Annotated[list[dict[str, Any]], merge_agent_messages]
    model_round: int
    loaded_skills: list[LoadedSkill]
    attachments: list[AttachmentAccess]
    knowledge_bases: list[KnowledgeBaseAccess]
    pending_agent_run_ids: list[str]
    tool_failure_counts: dict[str, int]
    document_call_counts: dict[str, int]
    finished: bool
    error: str | None


def new_agent_state(
    *,
    run_id: str = '',
    messages: list[dict[str, Any]] | None = None,
    loaded_skills: list[LoadedSkill] | None = None,
    attachments: list[AttachmentAccess] | None = None,
    knowledge_bases: list[KnowledgeBaseAccess] | None = None,
    pending_agent_run_ids: list[str] | None = None,
) -> AgentState:
    """构造一次 Agent Run 的完整初始 State，集中所有默认值。"""
    return {
        'version': STATE_VERSION,
        'run_id': run_id,
        'messages': [dict(message) for message in messages or []],
        'model_round': 0,
        'loaded_skills': list(loaded_skills or []),
        'attachments': list(attachments or []),
        'knowledge_bases': list(knowledge_bases or []),
        'pending_agent_run_ids': list(pending_agent_run_ids or []),
        'tool_failure_counts': {},
        'document_call_counts': {},
        'finished': False,
        'error': None,
    }


class AgentStateUpdate(TypedDict, total=False):
    """声明节点可返回的 State 更新字段。"""

    version: int
    run_id: str
    messages: list[dict[str, Any]] | dict[str, list[dict[str, Any]]]
    model_round: int
    loaded_skills: list[LoadedSkill]
    attachments: list[AttachmentAccess]
    knowledge_bases: list[KnowledgeBaseAccess]
    pending_agent_run_ids: list[str]
    tool_failure_counts: dict[str, int]
    document_call_counts: dict[str, int]
    finished: bool
    error: str | None
