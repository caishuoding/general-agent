"""验证主从工具注册表只复用安全公共工具。"""

from general_agent.agent.tools import MAIN_ONLY_TOOLS, ORCHESTRATION, WORKER_TOOLS
from general_agent.memory.tools import agent_tools, worker_tools


def test_worker_tools_reuse_shared_tools_without_memory_or_orchestration() -> None:
    """子智能体自动获得公共工具，但不会获得记忆写入和四个编排工具。"""
    main_names = {tool['function']['name'] for tool in agent_tools(knowledge=True)}
    worker_names = {tool['function']['name'] for tool in worker_tools(knowledge=True)}
    assert worker_names == WORKER_TOOLS
    assert not worker_names & MAIN_ONLY_TOOLS
    assert not worker_names & ORCHESTRATION
    assert worker_names < main_names
