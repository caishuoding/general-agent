"""集中管理发送给模型的系统提示词及资料消息组装。"""

from general_agent.prompts.system import (
    TOOL_DESCRIPTIONS,
    attachment_material,
    history_material,
    image_material_label,
    memory_failure,
    memory_material,
    summary_chunk_instruction,
    summary_final_instruction,
    tool_planner_instruction,
    user_response_instruction,
    web_query_instruction,
    web_results_material,
)

__all__ = [
    "TOOL_DESCRIPTIONS",
    "attachment_material",
    "history_material",
    "image_material_label",
    "memory_failure",
    "memory_material",
    "summary_chunk_instruction",
    "summary_final_instruction",
    "tool_planner_instruction",
    "user_response_instruction",
    "web_query_instruction",
    "web_results_material",
]
