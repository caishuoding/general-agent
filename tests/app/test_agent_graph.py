"""验证 LangGraph 智能体编排的关键路由。"""

import json
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

from general_agent.agent import AgentToolGraph
from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.infrastructure.provider import StreamChunk, ToolCallDelta
from general_agent.infrastructure.settings import StoredSettings
from general_agent.memory.automatic import MemoryCheckpoint


def settings() -> StoredSettings:
    """构造不会实际联网的模型配置。"""
    return StoredSettings(base_url="https://model.example/v1", api_key="test-key", model="test-model", revision="revision-1")


def checkpoint() -> MagicMock:
    """构造内存检查点，并使用真实可序列化约束检查步骤标识。"""
    saved: dict[str, Any] = {}
    result = MagicMock(spec=MemoryCheckpoint)
    result.message_id = uuid4()
    # 使用 JSON 构造稳定步骤键，同时检查实际业务检查点的序列化要求。
    result.key.side_effect = lambda phase, value: phase + ":" + json.dumps(value, sort_keys=True)
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


def test_default_tool_retry_limit_is_shared_by_every_tool() -> None:
    """默认工具图使用项目统一的五次失败重试，不允许单个工具另设默认值。"""
    assert LIMITS.tool_retries == 5
    assert AgentToolGraph().maximum_tool_retries == LIMITS.tool_retries


def test_time_tool_reads_fresh_clock_when_replaying_a_tool_call(monkeypatch: Any) -> None:
    """即使恢复时复用模型的调用决定，执行入口仍读取新时钟而不复用旧时间。"""
    from datetime import datetime, timezone

    from general_agent.memory.tools import MemoryTools, agent_tools

    clock = MagicMock()
    clock.now.side_effect = [
        datetime(2026, 10, 21, 16, 30, 45, tzinfo=timezone.utc),
        datetime(2026, 10, 21, 16, 31, 45, tzinfo=timezone.utc),
    ]
    monkeypatch.setattr('general_agent.agent.time.datetime', clock)
    saved = cast(MemoryCheckpoint, checkpoint())
    tools = MemoryTools(MagicMock(), MagicMock())
    results: list[str] = []
    calls = MagicMock()
    calls.stream.side_effect = [
        [StreamChunk(tool_call=ToolCallDelta(index=0, id='time-1', name='get_current_time', arguments='{}'))],
        [StreamChunk(content='已取得当前北京时间。')],
        [StreamChunk(content='已取得更新后的北京时间。')],
    ]
    for _ in range(2):
        result = AgentToolGraph().run(
            messages=[{'role': 'user', 'content': '现在几点？'}], settings=settings(), calls=calls,
            checkpoint=saved, tools=agent_tools(), execute_tool=tools.execute,
        )
        results.append(json.loads(result['messages'][-2]['content'])['datetime'])
    assert results == ['2026-10-22T00:30:45+08:00', '2026-10-22T00:31:45+08:00']


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


def tool_chunks(names: list[str], identifier: str) -> list[StreamChunk]:
    """构造一轮完整工具调用分片，用不同参数检查工具额度，并允许同一轮连续请求多个工具。"""
    return [StreamChunk(tool_call=ToolCallDelta(
        index=index, id=f'{identifier}-{index}', name=name,
        arguments=json.dumps({'query': identifier}),
    )) for index, name in enumerate(names)]


def web_tools() -> list[dict[str, Any]]:
    """提供两个可独立禁用的受控工具定义。"""
    return [{'type': 'function', 'function': {'name': name}}
            for name in ('search_web', 'fetch_web')]


def test_exhausted_tool_is_hidden_and_forged_call_is_blocked() -> None:
    """首次失败加五次重试后移除工具，告知模型且拦截再次调用，其他工具仍可用。"""
    from general_agent.infrastructure.provider import ModelFailure

    calls = MagicMock()
    calls.stream.side_effect = [
        *[tool_chunks(['search_web'], str(index)) for index in range(7)],
        tool_chunks(['fetch_web'], 'other'),
        [StreamChunk(content='根据已有结果回答。')],
    ]

    def execute(current: MemoryCheckpoint, name: str, arguments: dict[str, Any], config: StoredSettings) -> dict[str, Any]:
        """只让搜索失败，网页抓取正常返回。"""
        if name == 'search_web':
            raise ModelFailure('搜索服务暂时不可用。')
        return {'content': '已有网页资料'}

    executor = MagicMock(side_effect=execute)
    result = AgentToolGraph().run(
        messages=[{'role': 'user', 'content': '查询资料'}], settings=settings(), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()), tools=web_tools(), execute_tool=executor,
    )
    assert [call.args[1] for call in executor.call_args_list].count('search_web') == 6
    assert executor.call_count == 7
    assert result['answer'] == '根据已有结果回答。'
    assert result['tool_failures'] == {'search_web': 6}
    for index, call in enumerate(calls.stream.call_args_list):
        names = {tool['function']['name'] for tool in call.kwargs['tools']}
        assert names == ({'fetch_web', 'search_web'} if index < 6 else {'fetch_web'})
    exhausted = json.loads(calls.stream.call_args_list[6].args[1][-1]['content'])
    assert exhausted['retry_count'] == exhausted['retry_limit'] == 5
    assert exhausted['retry_exhausted'] is True
    assert exhausted['error'] == '搜索服务暂时不可用。'
    assert '本次回答中已禁用' in exhausted['message']
    blocked = json.loads(calls.stream.call_args_list[7].args[1][-1]['content'])
    assert blocked['retry_count'] == 5 and blocked['retry_exhausted'] is True


def test_fifth_retry_may_succeed_and_success_does_not_disable_tool() -> None:
    """第五次重试仍执行；成功调用不增加失败次数，不会提前禁用工具。"""
    calls = MagicMock()
    calls.stream.side_effect = [
        *[tool_chunks(['search_web'], str(index)) for index in range(7)],
        [StreamChunk(content='已取得结果。')],
    ]
    executor = MagicMock(side_effect=[*[ValueError('暂时失败') for _ in range(5)], {'results': []}, {'results': []}])
    result = AgentToolGraph().run(
        messages=[{'role': 'user', 'content': '查询'}], settings=settings(), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()), tools=web_tools(), execute_tool=executor,
    )
    assert executor.call_count == 7
    assert result['tool_failures'] == {'search_web': 5}
    assert all(len(call.kwargs['tools']) == 2 for call in calls.stream.call_args_list)
    assert result['answer'] == '已取得结果。'


def test_same_batch_cannot_exceed_retry_budget_and_all_tools_can_be_hidden() -> None:
    """同一批次也检查限额，工具全部禁用后不再向模型发送工具定义。"""
    calls = MagicMock()
    calls.stream.side_effect = [tool_chunks(['search_web'] * 8, 'batch'), [StreamChunk(content='无法查询。')]]
    executor = MagicMock(return_value={'error': '查询失败'})
    result = AgentToolGraph().run(
        messages=[{'role': 'user', 'content': '查询'}], settings=settings(), calls=calls,
        checkpoint=cast(MemoryCheckpoint, checkpoint()), tools=web_tools()[:1], execute_tool=executor,
    )
    assert executor.call_count == 6
    assert calls.stream.call_args_list[-1].kwargs['tools'] is None
    assert result['tool_failures'] == {'search_web': 6}
    assert result['answer'] == '无法查询。'


def test_retry_budget_replays_without_reexecuting_and_resets_for_new_answer() -> None:
    """同一回答恢复时复用结果并重建次数，新回答重置失败额度，同时保留成功步骤和模型决策缓存。"""
    graph = AgentToolGraph()
    saved = cast(MemoryCheckpoint, checkpoint())
    executor = MagicMock(side_effect=ValueError('失败'))
    calls = MagicMock()
    first_id = uuid4()
    for identifier in (first_id, first_id, uuid4()):
        calls.stream.side_effect = [
            *[tool_chunks(['search_web'], str(index)) for index in range(6)],
            [StreamChunk(content='已停止使用失败工具。')],
        ]
        result = graph.run(
            messages=[{'role': 'user', 'content': '查询'}], settings=settings(), calls=calls,
            checkpoint=saved, tools=web_tools(), execute_tool=executor, assistant_message_id=identifier,
        )
        assert result['tool_failures'] == {'search_web': 6}
    assert executor.call_count == 12
    assert calls.stream.call_count == 7
