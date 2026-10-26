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
    """提供异步创建、等待、补充和取消工具，不强制每个问题分工。"""
    definitions: list[tuple[str, str, type[BaseModel]]] = [
        ('spawn_agent', '当任务存在两个以上彼此独立、可并行完成并统一汇总的调查、比较、核验或审查分支时，为每个分支创建子智能体。一次调用只分派一个可独立验收的子任务，明确任务范围、证据和预期交付；简单问题或强依赖步骤由主智能体直接完成。创建后立即返回 ID，子任务在后台执行，你可以继续自己工作或创建其他子任务；instructions 只补充任务方法和重点，不能覆盖固定安全规则。', SpawnInput),
        ('wait_agents', '零参数等待当前回答任一子任务的新终态。已有结果立即交付；没有结果时挂起等待，不反复调用模型。无活动任务返回 idle。结果也会在模型轮次间自动交付，检查结论、证据与缺口。', WaitInput),
        ('send_agent_message', '给已完成的子智能体追加任务，延续其上下文并立即返回新执行 ID；instructions 可更新下一轮任务级方法，留空则沿用。运行中返回 agent_busy，不自动排队；之后用 wait_agents 取结果。', SendInput),
        ('cancel_agent', '取消不再需要的子任务，防止继续调用；已发请求仍可能计费。最终回答前等待需要的任务，取消其余任务。', CancelInput),
    ]
    return [{'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': model.model_json_schema()}} for name, description, model in definitions]
