"""在 Agent 内部状态与外部模型消息协议之间执行最小字段转换。"""

from typing import Any


def protocol_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """移除仅供恢复、压缩和路由使用的下划线字段及空工具调用。"""
    return [{key: value for key, value in message.items()
             if not key.startswith('_') and not (key == 'tool_calls' and not value)}
            for message in messages]
