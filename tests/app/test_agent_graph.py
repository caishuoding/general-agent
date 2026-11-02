"""通过唯一图验证模型工具循环、失败预算、消息准备与收尾。"""

import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, replace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from langgraph.runtime import Runtime

from general_agent.agent.context import AgentContext, AgentRole
from general_agent.agent.graph import build_agent_graph
from general_agent.agent.nodes import AgentNodes
from general_agent.agent.state import AgentState, merge_agent_messages, new_agent_state
from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelConnectionFailure, StreamChunk, ToolCallDelta
from general_agent.infrastructure.settings import StoredSettings


class GraphHarness:
    """以替身承接外部服务，实际执行五个节点及完整 State 路由。"""

    def __init__(self) -> None:
        """配置内存步骤缓存、无网络模型和可观察的业务提交。"""
        identifier, account, conversation, user, answer = (uuid4() for _ in range(5))
        self.context = AgentContext(
            role=AgentRole.MAIN, run_id=identifier,
            conversation_id=conversation, account_id=account,
            user_message_id=user, assistant_message_id=answer,
            settings=StoredSettings(base_url='https://model.example/v1', api_key='test-key',
                                    model='controlled', revision='revision-1'),
            operation=OperationContext(uuid4(), 'chat', conversation, account), delivery='stream',
        )
        self.services = MagicMock()
        self.services.attachment_access.return_value = []
        self.services.knowledge_base_access.return_value = []
        self.services.search.resolve.side_effect = self.resolve
        self.services.resolve_knowledge_citations.side_effect = self.resolve
        self.services.prepare_response.side_effect = self.prepare
        self.repository = MagicMock()
        self.connection = self.repository.database.connect.return_value.__enter__.return_value
        self.connection.execute.return_value.fetchone.return_value = None
        self.connection.execute.return_value.fetchall.return_value = []
        self.repository.sources.return_value = []
        self.checkpoint = MagicMock()
        self.checkpoint.main = True
        self.checkpoint.operation = self.context.operation
        self.checkpoint.run_id = identifier
        self.checkpoint.key.side_effect = self.key
        self.saved: dict[str, Any] = {}
        self.checkpoint.get.side_effect = self.saved.get
        self.checkpoint.put.side_effect = self.saved.__setitem__
        self.calls = self.services.memory.calls.bind.return_value
        self.calls.stream.return_value = [StreamChunk(content='最终回答')]
        self.compactor = MagicMock()
        self.compactor.prepare.side_effect = self.compact
        self.counter = MagicMock()
        self.counter.available.return_value = 100000
        self.counter.count.return_value = 0
        self.orchestrator = MagicMock()
        self.orchestrator.inbox.settle.return_value = True
        self.orchestrator.inbox.collect.return_value = {'status': 'idle', 'agents': []}
        self.nodes = AgentNodes(
            services=self.services, repository=self.repository, checkpoint=self.checkpoint,
            orchestrator=self.orchestrator,
            compactor=self.compactor, counter=self.counter, skill_runtime=None,
            run_child=MagicMock(),
        )
        self.state = new_agent_state(run_id=str(identifier), messages=[{
            'id': str(user), 'role': 'user', 'content': '当前问题', '_current_request': True,
        }])

    @staticmethod
    def key(phase: str, value: Any) -> str:
        """用可序列化输入生成稳定缓存键。"""
        return phase + ':' + json.dumps(value, sort_keys=True, default=str)

    @staticmethod
    def resolve(_scope: Any, answer: str) -> tuple[str, list[Any]]:
        """无外部来源时保持正文不变。"""
        return answer, []

    @staticmethod
    def prepare(_conversation: Any, _account: Any, _user: Any,
                _operation: Any, messages: list[dict[str, Any]]) -> Any:
        """返回节点实际传入的消息，使新一轮清理可被验证。"""
        return messages

    @staticmethod
    def compact(messages: Any, _tools: Any) -> Any:
        """默认保留消息，具体压缩测试可覆盖此行为。"""
        return messages

    def run(self) -> AgentState:
        """执行真实五节点图并返回最终 State。"""
        return cast(AgentState, build_agent_graph(self.nodes, None).invoke(
            self.state, {'recursion_limit': 200}, context=self.context))


def tool_chunks(names: list[str], prefix: str = 'call') -> list[StreamChunk]:
    """构造同一模型回复内的一批完整工具调用。"""
    return [StreamChunk(tool_call=ToolCallDelta(
        index=index, id=f'{prefix}:{index}', name=name, arguments='{}',
    )) for index, name in enumerate(names)]


def test_graph_prepares_streams_and_commits_in_one_cycle() -> None:
    """模型公开分片组成最终消息，并由唯一图提交和关闭根 Run。"""
    harness = GraphHarness()
    emitted: list[str] = []
    harness.context = replace(harness.context, on_content=emitted.append)
    harness.calls.stream.return_value = [StreamChunk(content='你好'), StreamChunk(content='世界')]
    state = harness.run()
    assert emitted == ['你好', '世界']
    assert state['messages'][-1]['content'] == '你好世界'
    assert state['messages'][-1]['id'] == str(harness.context.assistant_message_id)
    assert state['finished'] and not state['error']
    harness.repository.close.assert_called_once_with(harness.context.assistant_message_id)
    harness.services.tools.execute.assert_not_called()
    harness.orchestrator.inbox.collect.assert_not_called()


def test_context_is_immutable() -> None:
    """上下文固定身份不能在节点间被修改。"""
    with pytest.raises(FrozenInstanceError):
        setattr(GraphHarness().context, 'run_id', uuid4())


def test_new_turn_keeps_dialog_and_summary_but_discards_previous_tool_trace() -> None:
    """新一轮移除上一轮原始工具协议，同时保留对话正文和摘要。"""
    harness = GraphHarness()
    harness.state['messages'] = [
        {'role': 'user', 'content': '旧问题'},
        {'role': 'assistant', 'tool_calls': [{'id': 'old'}], 'content': ''},
        {'role': 'tool', 'content': '旧工具结果', 'tool_call_id': 'old'},
        {'role': 'assistant', 'content': '旧答案'},
        {'role': 'user', 'content': '摘要', '_context_summary': True},
        *harness.state['messages'],
    ]
    harness.run()
    sent = harness.calls.stream.call_args.args[1]
    assert [message['content'] for message in sent[1:]] == ['旧问题', '旧答案', '摘要', '当前问题']
    assert len([message for message in sent if message['role'] == 'system']) == 1


def test_context_failure_finalizes_without_model_or_previous_answer_leak() -> None:
    """准备失败同样收尾，流式失败不会拿上一轮答案覆盖当前消息。"""
    harness = GraphHarness()
    harness.state['messages'].insert(0, {'role': 'assistant', 'content': '上一轮答案'})
    harness.services.prepare_response.side_effect = ValueError('资料不可用')
    state = harness.run()
    assert state['error'] == '资料不可用'
    assert state['messages'][-1]['content'] == ''
    harness.calls.stream.assert_not_called()


def test_connection_interruption_resumes_from_visible_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """断线续写保留公开正文、恢复标记，并在续写前重新执行压缩。"""
    harness = GraphHarness()
    monkeypatch.setattr('general_agent.agent.nodes.time.sleep', MagicMock())

    def interrupted() -> Iterator[StreamChunk]:
        """产生公开部分正文后模拟连接中断。"""
        yield StreamChunk(content='前文')
        raise ModelConnectionFailure('连接中断')

    harness.calls.stream.side_effect = [interrupted(), [StreamChunk(content='续文')]]
    state = harness.run()
    assert state['messages'][-1]['content'] == '前文[[STREAM_RESUMED]]续文'
    retry = harness.calls.stream.call_args_list[1].args[1]
    assert retry[-2] == {'role': 'assistant', 'content': '前文'}
    assert '不要重复' in retry[-1]['content']
    assert harness.compactor.prepare.call_count == 2


def test_connection_exhaustion_keeps_partial_and_failed_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """耗尽续写次数后保留实际分片并提交失败终态。"""
    harness = GraphHarness()
    monkeypatch.setattr('general_agent.agent.nodes.time.sleep', MagicMock())

    def interrupted() -> Iterator[StreamChunk]:
        """仅首轮产生分片后中断。"""
        yield StreamChunk(content='部分正文')
        raise ModelConnectionFailure('连接中断')

    harness.calls.stream.side_effect = [interrupted(), ModelConnectionFailure('断线'), ModelConnectionFailure('断线')]
    state = harness.run()
    assert state['finished']
    assert state['messages'][-1]['content'] == '部分正文'
    assert state['error'] == '模型连接中断，自动恢复 2 次后仍未成功。'
    assert harness.calls.stream.call_count == LIMITS.stream_attempts


def test_streamed_tool_arguments_are_joined_before_execution() -> None:
    """工具名称与参数按流片段合并后只执行一次。"""
    harness = GraphHarness()
    harness.calls.stream.side_effect = [[
        StreamChunk(tool_call=ToolCallDelta(index=0, id='first', name='search_', arguments='{"query":')),
        StreamChunk(tool_call=ToolCallDelta(index=0, name='web', arguments='"x"}')),
    ], [StreamChunk(content='结论')]]
    harness.services.tools.execute.return_value = {'results': []}
    state = harness.run()
    assert state['messages'][-1]['content'] == '结论'
    assert harness.services.tools.execute.call_args.args[1:3] == ('search_web', {'query': 'x'})


@pytest.mark.parametrize('success_at_limit', [False, True])
def test_failure_limit_and_forged_calls_are_enforced(success_at_limit: bool) -> None:
    """第五次重试仍可成功，持续失败则隐藏工具并拒绝伪造调用。"""
    harness = GraphHarness()
    harness.calls.stream.side_effect = [
        *[tool_chunks(['search_web'], str(index)) for index in range(8)],
        [StreamChunk(content='结论')],
    ]
    if success_at_limit:
        harness.services.tools.execute.side_effect = [*[ValueError('失败') for _ in range(5)],
                                                       {'results': []}, {'results': []}, {'results': []}]
    else:
        harness.services.tools.execute.side_effect = ValueError('失败')
    state = harness.run()
    assert harness.services.tools.execute.call_count == (8 if success_at_limit else 6)
    assert state['tool_failure_counts']['search_web'] == (5 if success_at_limit else 6)
    tools = harness.calls.stream.call_args.kwargs['tools']
    assert ('search_web' in {tool['function']['name'] for tool in tools}) is success_at_limit


def test_same_batch_cannot_exceed_retry_budget() -> None:
    """同批工具逐个核对失败额度，超过预算的调用不执行。"""
    harness = GraphHarness()
    harness.calls.stream.side_effect = [tool_chunks(['search_web'] * 8), [StreamChunk(content='结论')]]
    harness.services.tools.execute.return_value = {'error': '失败'}
    state = harness.run()
    assert harness.services.tools.execute.call_count == 6
    assert state['tool_failure_counts'] == {'search_web': 6}


def test_clock_is_not_cached_on_step_replay() -> None:
    """时间工具每次回放读取实际时钟，不复用旧的时间结果。"""
    harness = GraphHarness()
    state: AgentState = {**harness.state, 'model_round': 1, 'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'clock', 'function': {
            'name': 'get_current_time', 'arguments': '{}'}}]},
    ]}
    harness.services.tools.execute.side_effect = [{'time': 'first'}, {'time': 'second'}]
    runtime = Runtime(context=harness.context)
    first = harness.nodes.execute_tools(state, runtime)
    second = harness.nodes.execute_tools(state, runtime)
    assert harness.services.tools.execute.call_count == 2
    assert first['messages'] != second['messages']


def test_tool_failure_cache_replays_without_reexecuting() -> None:
    """同一工具批次恢复时读取原结果，重新计算同一份失败次数。"""
    harness = GraphHarness()
    state: AgentState = {**harness.state, 'model_round': 1, 'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'search', 'function': {
            'name': 'search_web', 'arguments': '{}'}}]},
    ]}
    harness.services.tools.execute.return_value = {'error': '失败'}
    runtime = Runtime(context=harness.context)
    first = harness.nodes.execute_tools(state, runtime)
    second = harness.nodes.execute_tools(state, runtime)
    assert harness.services.tools.execute.call_count == 1
    assert first['tool_failure_counts'] == second['tool_failure_counts'] == {'search_web': 1}


def test_document_budget_uses_batch_failures_and_current_tool_messages() -> None:
    """同批普通工具耗尽后，文档预算排除该工具并包含已产生结果。"""
    harness = GraphHarness()
    document = {'id': str(uuid4()), 'filename': '资料.txt', 'kind': 'document'}
    harness.services.attachment_access.return_value = [document]
    harness.nodes.maximum_tool_retries = 0
    harness.calls.stream.side_effect = [tool_chunks(['search_web', 'read_document']), [StreamChunk(content='结论')]]
    harness.services.tools.execute.side_effect = [{'error': '搜索失败'}, {'content': []}]
    harness.run()
    budget_candidates = [call for call in harness.counter.available.call_args_list
                         if any(message.get('role') == 'tool' for message in call.args[1])]
    assert budget_candidates
    budget_call = budget_candidates[0]
    assert 'search_web' not in {tool['function']['name'] for tool in budget_call.args[2]}
    assert any(message.get('role') == 'tool' for message in budget_call.args[1])


def test_inbox_delivery_preserves_history_even_after_child_finishes() -> None:
    """已完成子任务仍可自动交付，结果追加而不覆盖当前问题。"""
    harness = GraphHarness()
    harness.connection.execute.return_value.fetchone.return_value = {'id': uuid4()}
    delivery = {'delivery_id': 'batch-1', 'agents': [{'result': '子任务结论'}]}
    harness.orchestrator.inbox.collect.return_value = delivery
    state = harness.run()
    assert any(message.get('content') == '当前问题' for message in state['messages'])
    assert any('子任务结论' in str(message.get('content')) for message in state['messages'])


def test_resumed_tools_are_rebuilt_from_access_list() -> None:
    """跳过上下文准备恢复时，文档和知识库工具依照 State 恢复。"""
    harness = GraphHarness()
    harness.state['attachments'] = [{'id': str(uuid4()), 'filename': '资料.txt', 'kind': 'document'}]
    harness.state['knowledge_bases'] = [{'id': str(uuid4()), 'name': '资料库'}]
    tools = harness.nodes._active_tools(harness.state)
    assert {'read_document', 'search_documents', 'search_knowledge'} <= {
        tool['function']['name'] for tool in tools}


def test_main_finalizer_preserves_background_command_poll_result() -> None:
    """后台命令未结束时，其真实轮询结果进入 State 后继续模型循环。"""
    harness = GraphHarness()
    harness.nodes.skill_runtime = MagicMock()
    harness.nodes.skill_runtime.poll.return_value = {'status': 'running'}
    harness.connection.execute.return_value.fetchone.return_value = {'id': uuid4()}
    harness.state['messages'].append({'id': f'assistant:{harness.context.run_id}:1',
                                      'role': 'assistant', 'content': '候选回答'})
    update = harness.nodes.finalize(harness.state, Runtime(context=harness.context))
    messages = merge_agent_messages(harness.state['messages'], update['messages'])
    assert any('后台制作命令实际状态' in str(message['content']) for message in messages)
    assert update['finished'] is False


def test_document_tools_have_independent_limits_including_same_batch_failures() -> None:
    """搜索和读取各有五次额度，失败也计数，同批超额不得进入执行器。"""
    harness = GraphHarness()
    harness.services.attachment_access.return_value = [
        {'id': str(uuid4()), 'filename': '资料.txt', 'kind': 'document'}]
    harness.calls.stream.side_effect = [
        tool_chunks(['search_documents'] * 7 + ['read_document'] * 6),
        [StreamChunk(content='已有资料')],
    ]
    harness.services.tools.execute.return_value = {'error': '可恢复错误'}
    state = harness.run()
    assert harness.services.tools.execute.call_count == 10
    assert state['document_call_counts'] == {'search_documents': 5, 'read_document': 5}
    assert not {'search_documents', 'read_document'} & {
        tool['function']['name'] for tool in harness.calls.stream.call_args.kwargs['tools']}
    assert not new_agent_state()['document_call_counts']


def test_invalid_document_json_uses_budget_without_business_execution() -> None:
    """文档畸形参数也消耗额度，第六次直接拒绝，模型仍可收尾。"""
    harness = GraphHarness()
    harness.services.attachment_access.return_value = [
        {'id': str(uuid4()), 'filename': '资料.txt', 'kind': 'document'}]
    harness.calls.stream.side_effect = [[StreamChunk(tool_call=ToolCallDelta(
        index=index, id=str(index), name='read_document', arguments='invalid json',
    )) for index in range(6)], [StreamChunk(content='参数无效')]]
    state = harness.run()
    assert state['document_call_counts'] == {'read_document': 5}
    harness.services.tools.execute.assert_not_called()


def test_wait_delivery_budget_excludes_tools_disabled_in_previous_round() -> None:
    """主动等待的交付预算使用当前工具集合，排除失败超限工具。"""
    harness = GraphHarness()
    harness.nodes.maximum_tool_retries = 0
    harness.calls.stream.side_effect = [tool_chunks(['search_web']), tool_chunks(['wait_agents'], 'wait'),
                                         [StreamChunk(content='结论')]]
    harness.services.tools.execute.return_value = {'error': '失败'}

    def deliver(_checkpoint: Any, _key: Any, validate: Any) -> dict[str, Any]:
        """通过真实交付校验回调检查剩余预算。"""
        result = {'status': 'idle', 'agents': []}
        validate(result)
        return result

    harness.orchestrator.inbox.wait.side_effect = deliver
    harness.run()
    budget_calls = [call for call in harness.counter.available.call_args_list
                    if 'subagent_results' in str(call.args[1])]
    assert budget_calls
    assert all('search_web' not in {tool['function']['name'] for tool in call.args[2]}
               for call in budget_calls)


def test_skill_tools_share_failure_limits_and_write_cache() -> None:
    """Skill 文件写入复用普通工具缓存，失败时遵守同一隐藏与拒绝策略。"""
    harness = GraphHarness()
    runtime = MagicMock()
    harness.nodes.skill_runtime = runtime
    harness.state['messages'] = [{'role': 'assistant', 'tool_calls': [{
        'id': 'write', 'function': {'name': 'write_work_file',
            'arguments': json.dumps({'path': 'notes.txt', 'content': '内容'})},
    }]}]
    harness.state['model_round'] = 1
    runtime.service.write_work.return_value = {'path': 'notes.txt'}
    first = harness.nodes.execute_tools(harness.state, Runtime(context=harness.context))
    second = harness.nodes.execute_tools(harness.state, Runtime(context=harness.context))
    assert first == second
    runtime.service.write_work.assert_called_once()
    harness.nodes.maximum_tool_retries = 0
    harness.state['tool_failure_counts'] = {'write_work_file': 1}
    blocked = harness.nodes.execute_tools(harness.state, Runtime(context=harness.context))
    assert '已禁用' in str(blocked['messages'])
    runtime.service.write_work.assert_called_once()


def test_graph_boundary_failure_uses_persisted_partial_and_shared_finalizer(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """图边界异常仍保留数据库草稿，经同一收尾提交失败且不伪造工具消息。"""
    from general_agent.agent.runner import AgentRunner

    harness = GraphHarness()
    runner = AgentRunner.__new__(AgentRunner)
    runner.services = harness.services
    runner.repository = harness.repository
    runner.orchestrator = harness.orchestrator
    runner.checkpoints = None
    runner.skill_runtime = MagicMock()
    harness.services.memory.directory = tmp_path
    graph = MagicMock()
    graph.invoke.side_effect = RuntimeError('注入图边界异常')
    harness.connection.execute.return_value.fetchone.return_value = {'content': '已生成部分正文'}
    monkeypatch.setattr('general_agent.agent.runner.TokenCounter.for_settings', MagicMock(return_value=harness.counter))
    monkeypatch.setattr('general_agent.agent.runner.ContextCompactor', MagicMock(return_value=harness.compactor))
    monkeypatch.setattr('general_agent.agent.runner.RunCheckpoint', MagicMock(return_value=harness.checkpoint))
    monkeypatch.setattr('general_agent.agent.runner.build_agent_graph', MagicMock(return_value=graph))
    result, state = runner._execute(harness.context, harness.state)
    assert result.output == '已生成部分正文'
    assert result.error == '回答执行中断，已保留部分内容，可以重试。'
    assert state['finished'] is True
    assert not any(message.get('role') == 'tool' for message in state['messages'])
