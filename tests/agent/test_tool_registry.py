"""验证主从工具注册表只复用安全公共工具。"""

from general_agent.agent.tools import MAIN_ONLY_TOOLS, ORCHESTRATION, WORKER_TOOLS, orchestration_tools
from general_agent.memory.tools import agent_tools, worker_tools
from general_agent.prompts import base_system_prompt
from general_agent.prompts.agents import SUBAGENT_PROMPT


def test_worker_tools_reuse_shared_tools_without_memory_or_orchestration() -> None:
    """子智能体自动获得公共工具，但不会获得记忆写入和四个编排工具。"""
    main_names = {tool['function']['name'] for tool in agent_tools(knowledge=True)}
    worker_names = {tool['function']['name'] for tool in worker_tools(knowledge=True)}
    assert worker_names == WORKER_TOOLS
    assert not worker_names & MAIN_ONLY_TOOLS
    assert not worker_names & ORCHESTRATION
    assert worker_names < main_names


def test_main_agent_policy_delegates_when_time_benefits_or_user_requests() -> None:
    """确认主提示仅在有时间收益或用户明确要求时分工，并保留简单任务直答。"""
    spawn = next(tool for tool in orchestration_tools()
                 if tool['function']['name'] == 'spawn_agent')
    assert '简单问题、单一事实' in base_system_prompt()
    assert '并行预计能缩短总耗时' in base_system_prompt()
    assert '用户明确要求分工或并行时优先遵从' in base_system_prompt()
    assert '主智能体' not in base_system_prompt()
    assert '不能创建子智能体' not in SUBAGENT_PROMPT
    assert '长期记忆' not in SUBAGENT_PROMPT
    assert '同一轮' in spawn['function']['description']
