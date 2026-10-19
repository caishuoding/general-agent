"""集中管理统一系统提示与模型资料组装。"""

from general_agent.prompts.system import (
    TOOL_DESCRIPTIONS,
    always_sent_prompt,
    attachment_context_fragment,
    attachment_data,
    base_system_prompt,
    compose_system_prompt,
    image_material_label,
    memory_context_fragment,
    memory_data,
    memory_failure,
    tool_result_fragment,
    tool_turn_fragment,
)

__all__ = [
    "TOOL_DESCRIPTIONS",
    "always_sent_prompt",
    "attachment_context_fragment",
    "attachment_data",
    "base_system_prompt",
    "compose_system_prompt",
    "image_material_label",
    "memory_context_fragment",
    "memory_data",
    "memory_failure",
    "tool_result_fragment",
    "tool_turn_fragment",
]
