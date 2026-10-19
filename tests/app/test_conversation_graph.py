"""验证完整对话执行确实由同一张 LangGraph 编排。"""

from unittest.mock import MagicMock
from uuid import uuid4

from general_agent.agent.conversation import ConversationContext, ConversationGraph
from general_agent.attachments.service import AttachmentContext, AttachmentService
from general_agent.conversation.execution import AnswerPreparation
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.settings import StoredSettings
from general_agent.memory.tools import ToolOutcome


def run_context() -> ConversationContext:
    """构造不含真实凭据和外部连接的回答运行上下文。"""
    conversation_id, user_id, answer_id, operation_id = uuid4(), uuid4(), uuid4(), uuid4()
    return ConversationContext(
        conversation_id=conversation_id,
        user_message_id=user_id,
        assistant_message_id=answer_id,
        settings=StoredSettings(base_url="https://model.example/v1", model="chat", api_key="test-key", revision="r1"),
        operation=OperationContext(operation_id, "chat", conversation_id),
        reserved_call_id=None,
        rows=[{"role": "user", "content": "你好"}],
        delivery="sync",
    )


def graph_dependencies() -> tuple[ConversationGraph, MagicMock, MagicMock, MagicMock, MagicMock]:
    """创建可观察节点调用的完整回答图及其领域模块替身。"""
    database = MagicMock(spec=Database)
    connection = MagicMock()
    database.connect.return_value.__enter__.return_value = connection
    calls = MagicMock(spec=CallGateway)
    execution = MagicMock()
    execution.search.resolve.side_effect = lambda operation, answer: (answer, [])
    attachments = MagicMock(spec=AttachmentService)
    attachments.prepare.return_value = AttachmentContext()
    graph = ConversationGraph(database, calls, execution, attachments)
    return graph, database, calls, execution, attachments


def test_complete_graph_streams_final_answer_after_tool_execution() -> None:
    """工具结果作为上下文进入最终回答，不在非流式规划回合提前结束。"""
    graph, database, calls, execution, attachments = graph_dependencies()
    execution.prepare.return_value = ToolOutcome("工具回答", True)
    execution.prepare_response.return_value = AnswerPreparation(
        messages=[{"role": "user", "content": "你好"}],
        material=AttachmentContext(),
        reserved=None,
    )
    context = run_context()

    result = graph.run(context)

    assert result["answer"] == "工具回答"
    execution.prepare_response.assert_called_once()
    connection = database.connect.return_value.__enter__.return_value
    assert any("UPDATE messages SET content" in item.args[0] for item in connection.execute.call_args_list)


def test_complete_graph_prepares_generates_and_finalizes_answer() -> None:
    """普通请求按 Agent、材料准备、最终模型和统一收尾的节点顺序执行。"""
    graph, database, calls, execution, attachments = graph_dependencies()
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

    result = graph.run(context)

    assert result["answer"] == "最终回答"
    execution.prepare_response.assert_called_once()
    connection = database.connect.return_value.__enter__.return_value
    terminal = [item for item in connection.execute.call_args_list if "UPDATE messages SET content" in item.args[0]]
    assert terminal and terminal[-1].args[1][0] == "最终回答"
