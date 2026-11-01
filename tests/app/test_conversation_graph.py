"""验证会话回答入口在同一 State 上准备、生成并提交回答。"""

from dataclasses import FrozenInstanceError, replace
from typing import Any, Callable, Literal, cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from general_agent.agent.conversation import ConversationContext, ConversationGraph
from general_agent.attachments.service import AttachmentContext
from general_agent.conversation.execution import AnswerPreparation, ChatExecution
from general_agent.infrastructure.checkpoints import STATE_VERSION
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelMessage
from general_agent.infrastructure.settings import StoredSettings


def run_context(delivery: Literal['sync', 'stream'] = 'sync') -> ConversationContext:
    """构造不含真实凭据和外部连接的回答运行上下文。"""
    conversation_id, account_id, user_id, answer_id, operation_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    return ConversationContext(
        conversation_id=conversation_id,
        account_id=account_id,
        user_message_id=user_id,
        assistant_message_id=answer_id,
        settings=StoredSettings(base_url='https://model.example/v1', model='chat', api_key='test-key', revision='r1'),
        operation=OperationContext(operation_id, 'chat', conversation_id),
        delivery=delivery,
    )


def user_rows(context: ConversationContext) -> list[dict[str, Any]]:
    """返回本轮入口实际从业务消息表读取的行。"""
    return [{'id': context.user_message_id, 'position': 1, 'role': 'user', 'content': '你好'}]


def graph_dependencies(answer: str = '最终回答') -> tuple[ConversationGraph, MagicMock, MagicMock]:
    """创建会话图和共享执行器替身，默认模拟一次完整模型回答。"""
    database = MagicMock(spec=Database)
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = {'position': 2}
    database.connect.return_value.__enter__.return_value = connection
    execution = MagicMock()
    execution.search = MagicMock()

    def no_committed_row() -> None:
        """默认不补充旧回答，提交窗口测试可单独覆盖查询结果。"""
        return None

    def resolve_answer(_operation: OperationContext, value: str) -> tuple[str, list[dict[str, Any]]]:
        """默认保留回答正文且不生成网页来源。"""
        return value, []

    def resolve_knowledge(_sources: list[dict[str, Any]], value: str) -> tuple[str, list[dict[str, Any]]]:
        """默认保留回答正文且不生成知识库引用。"""
        return value, []

    connection.execute.return_value.fetchone.side_effect = no_committed_row
    execution.search.resolve.side_effect = resolve_answer
    execution.resolve_knowledge_citations.side_effect = resolve_knowledge
    execution.material_attachment_ids.return_value = []
    execution.agent_runtime.repository.snapshot.return_value = {'attachments': [], 'knowledge': []}
    execution.agent_runtime.repository.sources.return_value = [
        {'key': 'K7', 'knowledge_base_id': 'base', 'document_id': 'document'},
        {'key': 'W39', 'url': 'https://example.org/weather'},
    ]

    def run_answer(*_args: object, **kwargs: object) -> dict[str, Any]:
        """通过公开回调模拟共享图完成一轮准备、模型回答和收尾。"""
        state = dict(cast(dict[str, Any], kwargs['initial_state']))
        raw_messages = cast(list[dict[str, Any]], kwargs.get('messages') or [])
        state['messages'] = [dict(message) for message in raw_messages]
        prepare_turn = cast(Callable[[dict[str, Any]], dict[str, Any]], kwargs['prepare_turn'])
        prepared = prepare_turn(state)
        if isinstance(prepared.get('messages'), dict):
            state['messages'] = prepared['messages']['replace']
        else:
            state['messages'] = prepared.get('messages', state['messages'])
        state.update({key: value for key, value in prepared.items() if key != 'messages'})
        if not state.get('failure'):
            callback = cast(Callable[[str], None], kwargs['on_content'])
            callback(answer)
            state['answer'] = answer
        finalize_turn = cast(Callable[[dict[str, Any]], dict[str, Any]], kwargs['finalize_turn'])
        finalized = finalize_turn(state)
        if isinstance(finalized.get('messages'), dict):
            state['messages'] = finalized['messages']['replace']
        else:
            state['messages'] = finalized.get('messages', state['messages'])
        state.update({key: value for key, value in finalized.items() if key != 'messages'})
        return dict(state)

    execution.run_answer.side_effect = run_answer
    return ConversationGraph(database, execution), database, execution


def test_run_prepares_generates_and_finalizes_answer() -> None:
    """普通请求依次经过上下文准备、模型回答和最终业务提交。"""
    graph, database, execution = graph_dependencies('最终回答')
    prepared: list[ModelMessage] = [{'role': 'system', 'content': '系统提示'}, {'role': 'user', 'content': '你好'}]
    execution.prepare_response.return_value = AnswerPreparation(messages=prepared, material=AttachmentContext())
    context = run_context()

    result = graph.run(context, user_rows(context))

    assert result == {'answer': '最终回答', 'failure': None}
    execution.prepare_response.assert_called_once()
    connection = database.connect.return_value.__enter__.return_value
    terminal = [call for call in connection.execute.call_args_list if 'UPDATE messages SET content' in call.args[0]]
    assert terminal and terminal[-1].args[1][0] == '最终回答'


def test_context_failure_is_finalized_without_calling_model() -> None:
    """准备失败进入统一收尾并保存失败终态，不生成模型回答。"""
    graph, database, execution = graph_dependencies()
    execution.prepare_response.side_effect = ValueError('图片已过期，请重新上传。')
    context = run_context()

    result = graph.run(context, user_rows(context))

    assert result == {'answer': '图片已过期，请重新上传。', 'failure': '图片已过期，请重新上传。'}
    execution.search.fail.assert_called_once_with(context.operation, result['failure'])
    connection = database.connect.return_value.__enter__.return_value
    terminal = [call for call in connection.execute.call_args_list if 'UPDATE messages SET content' in call.args[0]]
    assert terminal[-1].args[1][1] == 'failed'


def test_existing_thread_receives_only_the_new_user_message() -> None:
    """已有会话 State 时只向 reducer 提交本轮新增消息，由 checkpoint 保留旧上下文。"""
    checkpoints = MagicMock()
    checkpoints.values.return_value = {'version': STATE_VERSION, 'messages': [{'role': 'user', 'content': '旧消息'}]}
    database = MagicMock(spec=Database)
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = None
    database.connect.return_value.__enter__.return_value = connection
    execution = MagicMock()
    execution.search = MagicMock()

    def resolve_answer(_operation: OperationContext, answer: str) -> tuple[str, list[dict[str, Any]]]:
        """已有线程测试不需要网页核验。"""
        return answer, []

    def resolve_knowledge(_sources: list[dict[str, Any]], answer: str) -> tuple[str, list[dict[str, Any]]]:
        """已有线程测试不需要知识库核验。"""
        return answer, []

    def run_answer(*_args: object, **kwargs: object) -> dict[str, Any]:
        """模拟共享图直接返回，供断言本轮消息输入。"""
        initial = cast(dict[str, Any], kwargs['initial_state'])
        return {**initial, 'messages': kwargs['messages'], 'answer': '答', 'failure': None}

    execution.search.resolve.side_effect = resolve_answer
    execution.resolve_knowledge_citations.side_effect = resolve_knowledge
    execution.material_attachment_ids.return_value = []
    execution.agent_runtime.repository.snapshot.return_value = {}
    execution.run_answer.side_effect = run_answer
    graph = ConversationGraph(database, execution, checkpoints)
    context = run_context()
    rows = [{'id': uuid4(), 'position': 1, 'role': 'user', 'content': '旧消息'}, *user_rows(context)]

    graph.run(context, rows)

    submitted = execution.run_answer.call_args.kwargs['messages']
    assert submitted == [{**user_rows(context)[0], '_current_request': True}]


def test_runtime_context_is_immutable() -> None:
    """固定运行上下文不能写入回答或材料等可变结果。"""
    context = run_context()
    with pytest.raises(FrozenInstanceError):
        setattr(context, 'answer', '隐藏结果')


def test_model_failure_uses_shared_turn_finalizer() -> None:
    """模型失败从同一收尾回调保存部分正文和失败状态。"""
    graph, database, execution = graph_dependencies()
    execution.prepare_response.return_value = AnswerPreparation(
        messages=[{'role': 'user', 'content': '你好'}], material=AttachmentContext())

    def fail(*_args: object, **kwargs: object) -> dict[str, Any]:
        """模拟模型产生公开文字后断开连接。"""
        callback = cast(Callable[[str], None], kwargs['on_content'])
        callback('已经生成的部分回答')
        failure_handler = cast(Callable[[Exception, str], dict[str, Any]], kwargs['model_failure'])
        failure = failure_handler(RuntimeError('连接中断'), '已经生成的部分回答')
        initial = cast(dict[str, Any], kwargs['initial_state'])
        raw_messages = cast(list[dict[str, Any]], kwargs['messages'])
        state = {**initial, 'messages': raw_messages, **failure}
        finalize_turn = cast(Callable[[dict[str, Any]], dict[str, Any]], kwargs['finalize_turn'])
        return {**state, **finalize_turn(state)}

    execution.run_answer.side_effect = fail
    context = replace(run_context(), delivery='stream')

    result = graph.run(context, user_rows(context))

    assert result == {'answer': '已经生成的部分回答', 'failure': '连接中断'}


def test_graph_boundary_failure_uses_last_state() -> None:
    """共享执行边界异常时使用调用方已有 State 保存失败终态。"""
    graph, database, execution = graph_dependencies()
    execution.prepare_response.return_value = AnswerPreparation(
        messages=[{'role': 'user', 'content': '你好'}], material=AttachmentContext())
    execution.run_answer.side_effect = RuntimeError('图边界失败')
    context = replace(run_context(), delivery='stream')

    result = graph.run(context, user_rows(context))

    assert result['answer'] == ''
    assert result['failure'] == '回答执行中断，已保留部分内容，可以重试。'
    connection = database.connect.return_value.__enter__.return_value
    terminal = [call for call in connection.execute.call_args_list if "status='failed'" in call.args[0]]
    assert terminal[-1].args[1][0] == ''
