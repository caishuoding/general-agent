"""验证唯一 Agent State、静态工具注册和唯一图拓扑。"""

from unittest.mock import MagicMock

from general_agent.agent.context import AgentContext, AgentRole
from general_agent.agent.graph import build_agent_graph
from general_agent.agent.state import AgentState, merge_agent_messages, new_agent_state
from general_agent.agent.tool_registry import build_tool_registry
from general_agent.skills.tools import SKILL_TOOLS


def test_agent_state_has_only_explicit_runtime_fields() -> None:
    """State 只暴露图恢复、路由、额度控制与运行清单字段。"""
    assert set(AgentState.__annotations__) == {
        'version', 'run_id', 'messages', 'model_round', 'loaded_skills', 'attachments',
        'knowledge_bases', 'pending_agent_run_ids', 'tool_failure_counts',
        'document_call_counts', 'finished', 'error',
    }


def test_loaded_skill_paths_are_top_level_state_data() -> None:
    """加载 Skill 后可从 State 直接读取版本、根目录和入口文件。"""
    state = new_agent_state()
    state['loaded_skills'].append({
        'name': 'demo', 'version': '1.0.0',
        'root': '/opt/skills/demo', 'entry': '/opt/skills/demo/SKILL.md',
    })
    assert state['loaded_skills'][0]['root'] == '/opt/skills/demo'
    assert state['loaded_skills'][0]['entry'] == '/opt/skills/demo/SKILL.md'


def test_skill_tools_are_registered_for_main_before_any_skill_is_loaded() -> None:
    """Skill 工具属于主智能体静态工具集，不依赖 loaded_skills 才注册。"""
    registry = build_tool_registry(AgentRole.MAIN)
    main_names = registry.names
    assert len(registry.definitions) == len(main_names)
    subagent_names = build_tool_registry(AgentRole.SUBAGENT).names
    assert SKILL_TOOLS <= main_names
    assert not SKILL_TOOLS & subagent_names


def test_message_reducer_upserts_by_stable_id() -> None:
    """相同消息 ID 更新原消息，新 ID 追加，避免恢复时重复结果。"""
    current = [{'id': 'user:1', 'role': 'user', 'content': '旧'}]
    updated = merge_agent_messages(current, [
        {'id': 'user:1', 'role': 'user', 'content': '新'},
        {'id': 'tool:1', 'role': 'tool', 'content': '结果'},
    ])
    assert updated == [
        {'id': 'user:1', 'role': 'user', 'content': '新'},
        {'id': 'tool:1', 'role': 'tool', 'content': '结果'},
    ]


def test_main_and_subagent_share_exact_graph_topology() -> None:
    """唯一图始终包含五个节点和两条条件路由。"""
    nodes = MagicMock()
    drawing = build_agent_graph(nodes, None).get_graph()
    assert set(drawing.nodes) == {
        '__start__', '__end__', 'prepare_context', 'prepare_model',
        'call_model', 'execute_tools', 'finalize',
    }
    edges = {(edge.source, edge.target) for edge in drawing.edges}
    assert {
        ('__start__', 'prepare_context'),
        ('prepare_context', 'prepare_model'),
        ('prepare_model', 'call_model'),
        ('call_model', 'execute_tools'),
        ('call_model', 'finalize'),
        ('execute_tools', 'prepare_model'),
        ('finalize', '__end__'),
        ('finalize', 'prepare_model'),
    } <= edges


def test_agent_context_uses_agent_naming_and_run_scope() -> None:
    """运行上下文使用 AgentRole 和稳定 thread_id，不定义第二套 State。"""
    assert AgentRole.MAIN.value == 'main'
    assert AgentRole.SUBAGENT.value == 'subagent'
    assert {'role', 'run_id', 'settings'} <= set(AgentContext.__dataclass_fields__)
