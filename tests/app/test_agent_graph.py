"""验证 LangGraph 智能体编排的关键路由。"""

from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

from general_agent.agent import AgentToolGraph
from general_agent.infrastructure.provider import StreamChunk, ToolCallDelta
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


def test_graph_streams_answer_without_tools() -> None:
    """没有工具调用时，模型文字分片直接组成最终回答。"""
    calls = MagicMock()
    calls.stream.return_value = [StreamChunk(content="你好"), StreamChunk(content="，有什么需要？")]
    execute = MagicMock()
    emitted: list[str] = []

    result = AgentToolGraph().run(
        messages=[{"role": "user", "content": "你好"}], settings=settings(), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()), tools=[], execute_tool=execute,
        on_content=emitted.append,
    )

    assert result["handled"] is True
    assert result["answer"] == "你好，有什么需要？"
    assert emitted == ["你好", "，有什么需要？"]
    execute.assert_not_called()


def test_graph_accumulates_streamed_tool_call_before_continuing() -> None:
    """工具名称和参数字符串可跨多个 SSE 分片到达，执行后继续流式回答。"""
    calls = MagicMock()
    calls.stream.side_effect = [
        [
            StreamChunk(tool_call=ToolCallDelta(index=0, id="call-1", name="search_memories")),
            StreamChunk(tool_call=ToolCallDelta(index=0, arguments='{"query"')),
            StreamChunk(tool_call=ToolCallDelta(index=0, arguments=':"杭州"}')),
        ],
        [StreamChunk(content="你住在杭州。")],
    ]
    execute = MagicMock(return_value={"memories": [{"memory_id": "memory-1", "version": 1, "fact": "住在杭州"}]})
    emitted: list[str] = []

    result = AgentToolGraph().run(
        messages=[{"role": "user", "content": "我住在哪里？"}], settings=settings(), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()),
        tools=[{"type": "function", "function": {"name": "search_memories"}}],
        execute_tool=execute,
        on_content=emitted.append,
    )

    assert result["handled"] is True
    assert result["answer"] == "你住在杭州。"
    assert emitted == ["你住在杭州。"]
    assert result["messages"][-2]["role"] == "tool"
    assert "memory-1" in result["messages"][-2]["content"]
    second_messages = calls.stream.call_args_list[1].args[1]
    assert second_messages[-1]["role"] == "tool"
    assert "memory-1" in second_messages[-1]["content"]
