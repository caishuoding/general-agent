"""定义主智能体编排工具的严格参数及角色权限。"""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class SpawnInput(BaseModel):
    """限定子任务及交付说明，身份和权限由后端绑定。"""
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=80)
    task: str = Field(min_length=1, max_length=12000)
    context: str = Field(default='', max_length=16000)
    expected_output: str = Field(min_length=1, max_length=4000)


class WaitInput(BaseModel):
    """限定等待目标和时长，游标用于抑制重复结果。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    agent_ids: list[str] = Field(min_length=1, max_length=6)
    after_cursor: str | None = None
    timeout_seconds: int = Field(default=30, ge=0, le=30)


class SendInput(BaseModel):
    """仅向已有子智能体追加一条明确任务。"""
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    agent_id: str
    message: str = Field(min_length=1, max_length=12000)


class CancelInput(BaseModel):
    """取消指定子智能体当前的活动执行。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    agent_id: str


ORCHESTRATION = {'spawn_agent', 'wait_agents', 'send_agent_message', 'cancel_agent'}
WORKER_TOOLS = {'get_current_time', 'search_documents', 'read_document', 'search_knowledge', 'search_web', 'fetch_web'}


def orchestration_tools() -> list[dict[str, Any]]:
    """提供异步创建、等待、补充和取消工具，不强制每个问题分工。"""
    definitions: list[tuple[str, str, type[BaseModel]]] = [
        ('spawn_agent', '按需异步分派独立子任务，立即返回 ID。简单问题直接回答。任务应明确且可验收；子任务在后台执行，你可以继续自己工作或创建其他子任务。', SpawnInput),
        ('wait_agents', '等待任一指定子智能体的新结果；超时只返回状态，不取消任务。使用返回的游标避免重复取得全文。需要结果时再等待，收到结果后检查证据与缺口。', WaitInput),
        ('send_agent_message', '给已完成的子智能体追加任务，延续其上下文并立即返回新执行 ID。运行中返回 agent_busy，不自动排队；之后用 wait_agents 取结果。', SendInput),
        ('cancel_agent', '取消不再需要的子任务，防止继续调用；已发请求仍可能计费。最终回答前等待需要的任务，取消其余任务。', CancelInput),
    ]
    return [{'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': model.model_json_schema()}} for name, description, model in definitions]
