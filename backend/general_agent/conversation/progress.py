"""保存当前工具进度，让界面只展示最新状态而不记录完整执行轨迹。"""

from typing import TypedDict
from uuid import UUID

from psycopg.types.json import Jsonb

from general_agent.infrastructure.database import Database


class MessageProgress(TypedDict):
    """描述一个可公开给用户的当前处理状态。"""

    code: str
    label: str


PREPARING_PROGRESS: MessageProgress = {
    "code": "preparing",
    "label": "正在准备回答…",
}
DRAFTING_PROGRESS: MessageProgress = {
    "code": "drafting",
    "label": "正在整理回答…",
}
SUBAGENT_PROGRESS: MessageProgress = {
    "code": "subagent",
    "label": "正在处理子任务…",
}
TOOL_PROGRESS: dict[str, MessageProgress] = {
    "search_documents": {"code": "search_documents", "label": "正在检索资料…"},
    "read_document": {"code": "read_document", "label": "正在阅读资料…"},
    "search_knowledge": {"code": "search_knowledge", "label": "正在检索知识库…"},
    "search_web": {"code": "search_web", "label": "正在联网搜索…"},
    "fetch_web": {"code": "fetch_web", "label": "正在读取网页…"},
}


def connection_retry_progress(attempt: int, total: int) -> MessageProgress:
    """返回可公开的连接恢复进度，明确当前自动重试序号和总次数。"""
    return {
        "code": "connection_retrying",
        "label": f"连接中断，正在自动恢复（{attempt}/{total}）…",
    }


CONNECTION_RESUMED_PROGRESS: MessageProgress = {
    "code": "connection_resumed",
    "label": "连接已恢复，正在从断点继续…",
}


def tool_progress(name: str) -> MessageProgress | None:
    """返回关键工具对应的公开进度；未列出的普通工具不改变当前状态。"""
    return TOOL_PROGRESS.get(name)


def update_message_progress(database: Database, message_id: UUID, progress: MessageProgress) -> None:
    """覆盖生成中回答的最新进度；已完成、停止或失败的迟到更新不生效。"""
    with database.connect() as connection:
        connection.execute(
            "UPDATE messages SET progress=%s WHERE id=%s AND status='generating'",
            (Jsonb(progress), message_id),
        )


def clear_message_progress(database: Database, message_id: UUID) -> None:
    """清除回答终态的临时进度，界面完成后不再保留动态状态。"""
    with database.connect() as connection:
        connection.execute("UPDATE messages SET progress=NULL WHERE id=%s", (message_id,))
