"""验证 LangGraph 智能体编排的关键路由。"""

from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

from general_agent.agent import AgentToolGraph
from general_agent.infrastructure.provider import TokenUsage, ToolReply
from general_agent.infrastructure.settings import StoredSettings
from general_agent.memory.automatic import MemoryCheckpoint


def settings() -> StoredSettings:
    """构造不会实际联网的模型配置。"""
    return StoredSettings(base_url="https://model.example/v1", api_key="test-key", model="test-model", revision="revision-1")


def checkpoint() -> MagicMock:
    """构造只在内存记录图步骤的业务检查点替身。"""
    saved: dict[str, Any] = {}
    result = MagicMock(spec=MemoryCheckpoint)
    result.message_id = uuid4()
    result.key.side_effect = lambda phase, value: phase + ":" + repr(value)
    result.get.side_effect = saved.get
    result.put.side_effect = saved.__setitem__
    return result


def test_graph_returns_no_action_without_tools() -> None:
    """模型明确无需工具时，图结束且不误判为已处理请求。"""
    calls = MagicMock()
    calls.structured.return_value = ToolReply(message={"role": "assistant", "content": "NO_TOOL_ACTION"}, usage=TokenUsage())
    execute = MagicMock()

    result = AgentToolGraph().run(
        messages=[{"role": "user", "content": "你好"}], settings=settings(), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()), tools=[], execute_tool=execute,
    )

    assert result["handled"] is False
    assert result["answer"] is None
    execute.assert_not_called()


def test_graph_routes_tool_result_back_to_model() -> None:
    """模型发起工具后，图执行工具并把内部结果交给下一轮模型。"""
    calls = MagicMock()
    calls.structured.side_effect = [
        ToolReply(message={"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "search_memories", "arguments": '{"query":"杭州"}'},
        }]}, usage=TokenUsage()),
        ToolReply(message={"role": "assistant", "content": "你住在杭州。"}, usage=TokenUsage()),
    ]
    execute = MagicMock(return_value={"memories": [{"memory_id": "memory-1", "version": 1, "fact": "住在杭州"}]})

    result = AgentToolGraph().run(
        messages=[{"role": "user", "content": "我住在哪里？"}], settings=settings(), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()),
        tools=[{"type": "function", "function": {"name": "search_memories"}}], execute_tool=execute,
    )

    assert result["handled"] is True
    assert result["answer"] == "你住在杭州。"
    second_messages = calls.structured.call_args_list[1].args[1]
    assert second_messages[-1]["role"] == "tool"
    assert "memory-1" in second_messages[-1]["content"]


def test_graph_uses_explicit_model_answer_without_second_call() -> None:
    """模型给出明确澄清或直接答复时，不再重复调用最终回答模型。"""
    calls = MagicMock()
    calls.structured.return_value = ToolReply(message={"role": "assistant", "content": "你好，有什么需要？"}, usage=TokenUsage())

    result = AgentToolGraph().run(
        messages=[{"role": "user", "content": "你好"}], settings=settings(), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()), tools=[], execute_tool=MagicMock(),
    )

    assert result["handled"] is True
    assert result["answer"] == "你好，有什么需要？"
    calls.structured.assert_called_once()
