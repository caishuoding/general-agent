"""在 Agent 内部状态与外部模型消息协议之间执行最小字段转换。"""

from typing import Any


def protocol_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """移除内部消息 ID、恢复与压缩标记及空工具调用，保持模型协议干净。"""
    return [{key: value for key, value in message.items()
             if key != 'id' and not key.startswith('_') and not (key == 'tool_calls' and not value)}
            for message in messages]
