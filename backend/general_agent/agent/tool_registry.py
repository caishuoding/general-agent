"""构建主智能体和子智能体的静态工具注册表。"""

from dataclasses import dataclass
from typing import Any

from general_agent.agent.context import AgentRole
from general_agent.agent.tools import orchestration_tools
from general_agent.memory.tools import agent_tools, worker_tools
from general_agent.skills.tools import skill_tools


@dataclass(frozen=True)
class AgentToolRegistry:
    """保存一次 Run 不随 Skill 加载状态变化的工具定义。"""

    definitions: list[dict[str, Any]]

    @property
    def names(self) -> frozenset[str]:
        """返回当前 Run 注册的全部工具名称。"""
        return frozenset(str(tool['function']['name']) for tool in self.definitions)


def build_tool_registry(role: AgentRole, *, documents: bool = False,
                        knowledge: bool = False) -> AgentToolRegistry:
    """按角色和资源清单注册工具；Skill 工具始终存在，不随加载状态变化。"""
    if role is AgentRole.SUBAGENT:
        return AgentToolRegistry(worker_tools(knowledge=knowledge, documents=documents))
    return AgentToolRegistry([
        *agent_tools(knowledge=knowledge, documents=documents),
        *orchestration_tools(),
        *skill_tools(),
    ])
