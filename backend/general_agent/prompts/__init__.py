"""集中管理统一系统提示与模型资料组装。"""

from general_agent.prompts.system import (
    attachment_data,
    base_system_prompt,
    image_material_label,
    memory_data,
    memory_failure,
)
from general_agent.prompts.tools import TOOL_DESCRIPTIONS

__all__ = [
    "TOOL_DESCRIPTIONS",
    "attachment_data",
    "base_system_prompt",
    "image_material_label",
    "memory_data",
    "memory_failure",
]
