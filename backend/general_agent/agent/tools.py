"""定义主智能体编排工具的严格参数及角色权限。"""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from general_agent.prompts.tools import TOOL_DESCRIPTIONS


class SpawnInput(BaseModel):
    """限定子任务及交付说明，身份和权限由后端绑定。"""
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=80)
    task: str = Field(min_length=1, max_length=12000)
    context: str = Field(default='', max_length=16000)
    expected_output: str = Field(min_length=1, max_length=4000)
    instructions: str = Field(default='', max_length=4000)


class WaitInput(BaseModel):
    """等待范围与期限由后端绑定，模型无需提供目标、游标或轮询时长。"""
    model_config = ConfigDict(extra='forbid', strict=True)


class SendInput(BaseModel):
    """仅向已有子智能体追加一条明确任务。"""
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    agent_id: str
    message: str = Field(min_length=1, max_length=12000)
    instructions: str | None = Field(default=None, max_length=4000)


class CancelInput(BaseModel):
    """取消指定子智能体当前的活动执行。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    agent_id: str


ORCHESTRATION = {'spawn_agent', 'wait_agents', 'send_agent_message', 'cancel_agent'}
SHARED_TOOLS = {'get_current_time', 'search_documents', 'read_document', 'search_knowledge', 'search_web', 'fetch_web'}
MAIN_ONLY_TOOLS = {'add_memory', 'search_memories', 'update_memory', 'delete_memory'}
WORKER_TOOLS = SHARED_TOOLS


def orchestration_tools() -> list[dict[str, Any]]:
    """返回四个编排工具的模型定义；无需输入，创建或等待的实际执行留给后端。"""
    definitions: list[tuple[str, str, type[BaseModel]]] = [
        ('spawn_agent', TOOL_DESCRIPTIONS['spawn_agent'], SpawnInput),
        ('wait_agents', TOOL_DESCRIPTIONS['wait_agents'], WaitInput),
        ('send_agent_message', TOOL_DESCRIPTIONS['send_agent_message'], SendInput),
        ('cancel_agent', TOOL_DESCRIPTIONS['cancel_agent'], CancelInput),
    ]
    return [{'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': model.model_json_schema()}} for name, description, model in definitions]
