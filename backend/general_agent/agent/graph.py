"""构建主智能体和子智能体共用的唯一 LangGraph 拓扑。"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from general_agent.agent.context import AgentContext
from general_agent.agent.nodes import (
    AgentNodes,
    route_after_finalize,
    route_after_model,
)
from general_agent.agent.state import AgentState


def build_agent_graph(
    nodes: AgentNodes,
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> Any:
    """构建唯一五节点 Agent 图，主从只通过 AgentContext.role 区分行为。"""
    builder = StateGraph(AgentState, context_schema=AgentContext)
    builder.add_node('prepare_context', nodes.prepare_context)
    builder.add_node('prepare_model', nodes.prepare_model)
    builder.add_node('call_model', nodes.call_model)
    builder.add_node('execute_tools', nodes.execute_tools)
    builder.add_node('finalize', nodes.finalize)
    builder.add_edge(START, 'prepare_context')
    builder.add_edge('prepare_context', 'prepare_model')
    builder.add_edge('prepare_model', 'call_model')
    builder.add_conditional_edges(
        'call_model',
        route_after_model,
        {'execute_tools': 'execute_tools', 'finalize': 'finalize'},
    )
    builder.add_edge('execute_tools', 'prepare_model')
    builder.add_conditional_edges(
        'finalize',
        route_after_finalize,
        {'prepare_model': 'prepare_model', '__end__': END},
    )
    return builder.compile(checkpointer=checkpointer)
