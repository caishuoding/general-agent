"""定义一次 Agent Run 返回给聊天入口的稳定结果。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentResult:
    """保存本次 Run 的最终公开文本和致命错误。"""

    output: str | None
    error: str | None
