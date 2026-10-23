"""验证完整对话执行确实由同一张 LangGraph 编排。"""

from dataclasses import FrozenInstanceError, replace
import json
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from langsmith import Client, trace, tracing_context

from general_agent.agent.conversation import ConversationContext, ConversationGraph, ConversationState
from general_agent.attachments.service import AttachmentContext
from general_agent.conversation.execution import AnswerPreparation
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.settings import StoredSettings
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.conversation.execution import ChatExecution
from general_agent.memory.tools import ToolOutcome


def run_context() -> ConversationContext:
    """构造不含真实凭据和外部连接的回答运行上下文。"""
    conversation_id, account_id, user_id, answer_id, operation_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    return ConversationContext(
        conversation_id=conversation_id,
        account_id=account_id,
        user_message_id=user_id,
        assistant_message_id=answer_id,
        settings=StoredSettings(base_url="https://model.example/v1", model="chat", api_key="test-key", revision="r1"),
        operation=OperationContext(operation_id, "chat", conversation_id),
        delivery="sync",
    )


def graph_dependencies() -> tuple[ConversationGraph, MagicMock, MagicMock]:
    """创建三步回答图及上下文准备和业务存储替身，不设置独立意图路由。"""
    database = MagicMock(spec=Database)
    connection = MagicMock()
    database.connect.return_value.__enter__.return_value = connection
    execution = MagicMock(spec=ChatExecution)
    execution.search = MagicMock()

    def resolve(operation: OperationContext, answer: str) -> tuple[str, list[dict[str, object]]]:
        """默认保留回答，网页核验专用场景可覆盖这一边界。"""
        return answer, []

    execution.search.resolve.side_effect = resolve
    execution.resolve_knowledge_citations.side_effect = lambda _messages, answer: (answer, [])
    execution.material_attachment_ids.return_value = []
    graph = ConversationGraph(database, execution)
    return graph, database, execution


def test_complete_graph_streams_final_answer_after_tool_execution() -> None:
    """工具结果作为上下文进入最终回答，不在非流式规划回合提前结束。"""
    graph, database, execution = graph_dependencies()
    execution.prepare.return_value = ToolOutcome("工具回答", True)
    execution.prepare_response.return_value = AnswerPreparation(
        messages=[{"role": "user", "content": "你好"}],
        material=AttachmentContext(),
        reserved=None,
    )
    context = run_context()

    result = graph.run(context, [{'role': 'user', 'content': '你好'}])

    assert result["answer"] == "工具回答"
    execution.prepare_response.assert_called_once()
    connection = database.connect.return_value.__enter__.return_value
    assert any("UPDATE messages SET content" in item.args[0] for item in connection.execute.call_args_list)


def test_complete_graph_prepares_generates_and_finalizes_answer() -> None:
    """普通请求只经过上下文准备、模型工具循环和统一收尾。"""
    graph, database, execution = graph_dependencies()
    def prepare(*args: object, **kwargs: object) -> ToolOutcome:
        """模拟模型流式输出最终回答并返回同一文本。"""
        on_content = kwargs["on_content"]
        assert callable(on_content)
        on_content("最终回答")
        return ToolOutcome("最终回答", False)

    execution.prepare.side_effect = prepare
    execution.prepare_response.return_value = AnswerPreparation(
        messages=[{"role": "user", "content": "你好"}],
        material=AttachmentContext(),
        reserved=None,
    )
    context = run_context()

    result = graph.run(context, [{'role': 'user', 'content': '你好'}])

    assert result["answer"] == "最终回答"
    execution.prepare_response.assert_called_once()
    connection = database.connect.return_value.__enter__.return_value
    terminal = [item for item in connection.execute.call_args_list if "UPDATE messages SET content" in item.args[0]]
    assert terminal and terminal[-1].args[1][0] == "最终回答"


def test_context_failure_is_finalized_without_calling_model() -> None:
    """合并准备步骤后，附件或上下文失败仍保存失败状态且不发送模型请求。"""
    graph, database, execution = graph_dependencies()
    execution.prepare_response.side_effect = ValueError('图片已过期，请重新上传。')
    context = run_context()

    result = graph.run(context, [{'role': 'user', 'content': '你好'}])

    assert result == {'answer': '图片已过期，请重新上传。', 'failure': '图片已过期，请重新上传。'}
    execution.prepare.assert_not_called()
    execution.search.fail.assert_called_once_with(context.operation, result['failure'])
    connection = database.connect.return_value.__enter__.return_value
    terminal = [item for item in connection.execute.call_args_list if 'UPDATE messages SET content' in item.args[0]]
    assert terminal[-1].args[1][1] == 'failed'


def initial_state() -> ConversationState:
    """提供节点追踪测试的实际图输入，身份及私密配置只由 context 注入。"""
    return {'messages': [{'role': 'user', 'content': '你好'}], 'source_attachment_ids': [],
            'reserved_call_id': None, 'answer': '', 'failure': None, 'used_tools': False,
            'web_sources': [], 'knowledge_sources': []}


def test_nodes_share_preparation_tools_and_final_answer_through_state() -> None:
    """实际节点更新包含准备材料、工具消息和核验后回答，且不重复历史或暴露配置。"""
    graph, database, execution = graph_dependencies()
    source, reserved = uuid4(), uuid4()
    prepared: list[ModelMessage] = [{'role': 'system', 'content': '系统提示'}, {'role': 'user', 'content': '你好'}]
    execution.prepare_response.return_value = AnswerPreparation(messages=prepared, material=AttachmentContext(), reserved=reserved)
    execution.material_attachment_ids.return_value = [source]
    tool_messages: list[dict[str, Any]] = [*[dict(message) for message in prepared],
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-1'}]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': '实际工具结果'},
        {'role': 'assistant', 'content': '待核验回答'}]
    execution.prepare.return_value = ToolOutcome('待核验回答', True, tool_messages)
    execution.search.resolve.side_effect = None
    sources = [{'key': 'W1', 'url': 'https://example.com', 'title': '实际来源'}]
    execution.search.resolve.return_value = ('核验后的回答', sources)
    context = run_context()
    original = initial_state()

    updates = list(graph.compiled.stream(original, context=context, stream_mode='updates'))

    assert [list(update) for update in updates] == [['prepare_context'], ['agent'], ['finalize']]
    preparation, agent, final = [next(iter(update.values())) for update in updates]
    assert preparation['messages'] == prepared
    assert preparation['source_attachment_ids'] == [str(source)]
    assert preparation['reserved_call_id'] == reserved
    assert execution.prepare.call_args.args[5] == reserved
    assert execution.prepare.call_args.kwargs['messages'] == prepared
    assert execution.prepare.call_args.kwargs['source_attachment_ids'] == [source]
    assert agent['messages'] == tool_messages and agent['used_tools'] is True
    assert final['answer'] == '核验后的回答' and final['web_sources'] == sources
    assert final['messages'][-1]['content'] == '核验后的回答'
    assert len(final['messages']) == len(tool_messages)
    assert tool_messages[-1]['content'] == '待核验回答'
    assert original['messages'] == [{'role': 'user', 'content': '你好'}]
    serialized = json.dumps(updates, ensure_ascii=False, default=str)
    assert context.settings.api_key not in serialized
    assert not hasattr(context, 'preparation') and not hasattr(context, 'answer')
    assert graph.compiled.checkpointer is None


def test_runtime_context_is_immutable() -> None:
    """固定运行上下文不能再被节点写入隐藏回答状态。"""
    context = run_context()
    with pytest.raises(FrozenInstanceError):
        setattr(context, 'answer', '隐藏结果')


def test_graph_trace_exposes_state_updates_without_runtime_credentials() -> None:
    """通过真实本地追踪 SDK 核对节点材料和回答可见，运行配置密钥不进入输入输出。"""
    graph, database, execution = graph_dependencies()
    prepared: list[ModelMessage] = [{'role': 'system', 'content': '系统提示'}, {'role': 'user', 'content': '你好'}]
    execution.prepare_response.return_value = AnswerPreparation(messages=prepared, material=AttachmentContext(), reserved=None)
    execution.prepare.return_value = ToolOutcome('实际回答', False)
    context = run_context()
    client = Client(api_url='http://localhost:1', api_key='local-test', info={}, auto_batch_tracing=False)

    # 只观察真实 SDK 追踪树，禁用回调的网络投递，测试不需要外部追踪服务。
    with patch.object(Client, 'create_run'), patch.object(Client, 'update_run'), tracing_context(enabled='local', client=client), trace('state-sharing-test', inputs={}) as parent:
        result = graph.run(context, [{'role': 'user', 'content': '你好'}])

    assert result['answer'] == '实际回答'
    runs = list(parent.child_runs)
    for run in runs:
        runs.extend(run.child_runs)
    nodes = {run.name: run for run in runs if run.name in ('prepare_context', 'agent', 'finalize')}
    assert set(nodes) == {'prepare_context', 'agent', 'finalize'}
    assert nodes['prepare_context'].outputs is not None
    assert nodes['prepare_context'].outputs['messages'] == prepared
    assert nodes['agent'].inputs['messages'] == prepared
    assert nodes['agent'].outputs is not None and nodes['agent'].outputs['answer'] == '实际回答'
    assert context.settings.api_key not in json.dumps([(run.inputs, run.outputs) for run in runs], default=str)


def test_streaming_failure_returns_partial_answer_in_state() -> None:
    """模型中断后将部分正文通过 State 传给收尾，并保存失败而非丢弃文字。"""
    graph, database, execution = graph_dependencies()
    execution.prepare_response.return_value = AnswerPreparation(
        messages=[{'role': 'user', 'content': '你好'}], material=AttachmentContext(), reserved=None)

    def fail_after_content(*args: object, **kwargs: object) -> ToolOutcome:
        """模拟一次已有文字分片的真实模型错误。"""
        callback = kwargs['on_content']
        assert callable(callback)
        callback('已经生成的部分回答')
        raise ModelFailure('连接中断')

    execution.prepare.side_effect = fail_after_content
    context = replace(run_context(), delivery='stream')

    result = graph.run(context, [{'role': 'user', 'content': '你好'}])

    assert result == {'answer': '已经生成的部分回答', 'failure': '连接中断'}
    connection = database.connect.return_value.__enter__.return_value
    terminal = [call for call in connection.execute.call_args_list if 'web_sources=' in call.args[0]]
    assert terminal[-1].args[1][:3] == ('已经生成的部分回答', 'failed', '连接中断')


def test_graph_boundary_failure_uses_last_state() -> None:
    """收尾节点意外失败时，边界处理从最新框架 State 保留已生成回答。"""
    graph, database, execution = graph_dependencies()
    execution.prepare_response.return_value = AnswerPreparation(
        messages=[{'role': 'user', 'content': '你好'}], material=AttachmentContext(), reserved=None)
    execution.prepare.return_value = ToolOutcome('已经完成的模型回答', False)
    execution.search.resolve.side_effect = RuntimeError('收尾边界失败')
    context = replace(run_context(), delivery='stream')

    result = graph.run(context, [{'role': 'user', 'content': '你好'}])

    assert result['answer'] == '已经完成的模型回答'
    assert result['failure'] == '回答执行中断，已保留部分内容，可以重试。'
    connection = database.connect.return_value.__enter__.return_value
    terminal = [call for call in connection.execute.call_args_list if "status='failed'" in call.args[0]]
    assert terminal[-1].args[1][0] == result['answer']
