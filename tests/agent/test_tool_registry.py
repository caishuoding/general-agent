"""验证主从工具注册表只复用安全公共工具。"""

from general_agent.agent.tools import MAIN_ONLY_TOOLS, ORCHESTRATION, WORKER_TOOLS, orchestration_tools
from general_agent.memory.tools import agent_tools, worker_tools
from general_agent.prompts.agents import MAIN_AGENT_PROMPT


def test_worker_tools_reuse_shared_tools_without_memory_or_orchestration() -> None:
    """子智能体自动获得公共工具，但不会获得记忆写入和四个编排工具。"""
    main_names = {tool['function']['name'] for tool in agent_tools(knowledge=True)}
    worker_names = {tool['function']['name'] for tool in worker_tools(knowledge=True)}
    assert worker_names == WORKER_TOOLS
    assert not worker_names & MAIN_ONLY_TOOLS
    assert not worker_names & ORCHESTRATION
    assert worker_names < main_names


def test_main_agent_policy_defaults_to_parallel_independent_branches() -> None:
    """系统提示与工具说明共同要求独立分支默认分工，同时保留简单和依赖任务直答。"""
    spawn = next(tool for tool in orchestration_tools()
                 if tool['function']['name'] == 'spawn_agent')
    assert '默认创建 2～3 个子智能体并行处理' in MAIN_AGENT_PROMPT
    assert '至少两个可以并行完成' in MAIN_AGENT_PROMPT
    assert '需要操作同一份共享状态' in MAIN_AGENT_PROMPT
    assert '一次调用只分派一个可独立验收的子任务' in spawn['function']['description']
    assert '简单问题或强依赖步骤由主智能体直接完成' in spawn['function']['description']
