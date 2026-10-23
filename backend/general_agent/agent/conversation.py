"""用 LangGraph State 统一传递同步与流式回答的执行数据。"""

from dataclasses import dataclass
import logging
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from psycopg.types.json import Jsonb

from general_agent.conversation.execution import ChatExecution
from general_agent.conversation.progress import DRAFTING_PROGRESS, MessageProgress, update_message_progress
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.infrastructure.settings import StoredSettings

LOGGER = logging.getLogger(__name__)


class ConversationState(TypedDict):
    """共享本次回答的完整消息快照、材料来源、回答和错误，不携带凭据或连接。"""

    messages: list[dict[str, Any]]
    source_attachment_ids: list[str]
    reserved_call_id: UUID | None
    answer: str
    failure: str | None
    used_tools: bool
    web_sources: list[dict[str, object]]
    knowledge_sources: list[dict[str, object]]


class ConversationResult(TypedDict):
    """向聊天入口返回 State 中的最终回答和失败说明。"""

    answer: str
    failure: str | None


@dataclass(frozen=True)
class ConversationContext:
    """提供本次运行固定身份、私密模型配置及传输方式，不保存节点计算结果。"""

    conversation_id: UUID
    account_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    settings: StoredSettings
    operation: OperationContext
    delivery: Literal['sync', 'stream']


class ConversationGraph:
    """通过节点返回的 State 更新编排回答，业务持久化独立于运行中数据传递。"""

    def __init__(self, database: Database, execution: ChatExecution) -> None:
        """编译无持久化检查点的三步图，消息使用完整快照覆盖以免重复追加历史。"""
        self.database = database
        self.execution = execution
        builder = StateGraph(ConversationState, context_schema=ConversationContext)
        builder.add_node('prepare_context', self.prepare_context)
        builder.add_node('agent', self.agent)
        builder.add_node('finalize', self.finalize)
        builder.add_edge(START, 'prepare_context')
        builder.add_edge('prepare_context', 'agent')
        builder.add_edge('agent', 'finalize')
        builder.add_edge('finalize', END)
        self.compiled = builder.compile(name='conversation')

    def run(self, context: ConversationContext, rows: list[dict[str, Any]], reserved_call_id: UUID | None = None) -> ConversationResult:
        """把历史及当前用户消息作为初始 State，固定配置通过 context 注入。"""
        initial: ConversationState = {
            'messages': [{'role': str(row['role']), 'content': str(row['content'])} for row in rows],
            'source_attachment_ids': [],
            'reserved_call_id': reserved_call_id,
            'answer': '',
            'failure': None,
            'used_tools': False,
            'web_sources': [],
            'knowledge_sources': [],
        }
        return self.invoke(initial, context)

    def invoke(self, graph_input: ConversationState, context: ConversationContext) -> ConversationResult:
        """观察框架生成的最新 State，图边界失败时据此收尾，不借用可变运行上下文。"""
        LOGGER.info('event=graph_start conversation_id=%s message_id=%s delivery=%s', context.conversation_id, context.assistant_message_id, context.delivery)
        state = graph_input
        try:
            for snapshot in self.compiled.stream(
                graph_input,
                {
                    'configurable': {'thread_id': str(context.assistant_message_id)},
                    'metadata': {'conversation_id': str(context.conversation_id),
                                 'assistant_message_id': str(context.assistant_message_id),
                                 'session_id': str(context.conversation_id)},
                },
                context=context,
                stream_mode='values',
            ):
                state = cast(ConversationState, snapshot)
            LOGGER.info('event=graph_end message_id=%s failure=%s', context.assistant_message_id, bool(state['failure']))
            return {'answer': state['answer'], 'failure': state['failure']}
        except Exception:
            LOGGER.exception('LangGraph 对话执行失败 conversation_id=%s message_id=%s', context.conversation_id, context.assistant_message_id)
            return self.fail_terminal(context, state, self.unexpected_failure(context.delivery))

    def prepare_context(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """返回准备好的模型消息、来源标识和调用预留，不修改固定运行上下文。"""
        context = runtime.context
        try:
            preparation = self.execution.prepare_response(
                context.conversation_id,
                context.account_id,
                context.user_message_id,
                context.assistant_message_id,
                context.settings,
                context.operation,
                state['reserved_call_id'],
                state['messages'],
            )
            LOGGER.info('event=node prepare_context message_id=%s', context.assistant_message_id)
            return {
                'messages': preparation.messages,
                'source_attachment_ids': [str(identifier) for identifier in self.execution.material_attachment_ids(preparation.material)],
                'reserved_call_id': preparation.reserved,
            }
        except (ValueError, ModelFailure) as error:
            return self.failure_update(context, str(error))
        except Exception:
            LOGGER.exception('LangGraph 上下文准备失败 conversation_id=%s message_id=%s', context.conversation_id, context.assistant_message_id)
            return self.failure_update(context, self.unexpected_failure(context.delivery))

    def agent(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """从 State 读取准备结果，返回工具循环消息和回答；失败时返回已产生的部分正文。"""
        if state['failure']:
            return {}
        context = runtime.context
        chunks: list[str] = []
        progress_started = False

        def on_progress(progress: MessageProgress) -> None:
            """只在流式入口写入最新工具状态，同步调用不需要临时进度。"""
            if context.delivery == 'stream':
                update_message_progress(self.database, context.assistant_message_id, progress)

        def on_content(text: str) -> None:
            """缓冲本节点文字并更新流式 UI，节点完成后再把回答作为 State 更新返回。"""
            nonlocal progress_started
            if not text:
                return
            chunks.append(text)
            if context.delivery == 'stream':
                if not progress_started:
                    on_progress(DRAFTING_PROGRESS)
                    progress_started = True
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE messages SET content=content || %s WHERE id=%s AND status='generating'",
                        (text, context.assistant_message_id),
                    )

        try:
            outcome = self.execution.prepare(
                context.conversation_id,
                context.account_id,
                context.user_message_id,
                str(state['messages'][-1]['content']),
                context.operation,
                state['reserved_call_id'],
                context.settings,
                messages=cast(list[ModelMessage], state['messages']),
                assistant_message_id=context.assistant_message_id,
                on_content=on_content,
                on_progress=on_progress,
                source_attachment_ids=[UUID(identifier) for identifier in state['source_attachment_ids']],
            )
            answer = outcome.answer or ''.join(chunks)
            if not answer:
                raise ValueError('模型没有返回最终回答。')
            LOGGER.info('event=node agent message_id=%s used_tools=%s characters=%s', context.assistant_message_id, outcome.used_tools, len(answer))
            return {'answer': answer, 'used_tools': outcome.used_tools,
                    'messages': outcome.messages if outcome.messages is not None else self.answered_messages(state['messages'], answer)}
        except (ValueError, ModelFailure) as error:
            return self.failure_update(context, str(error), ''.join(chunks))
        except Exception:
            LOGGER.exception('LangGraph 工具节点失败 conversation_id=%s message_id=%s', context.conversation_id, context.assistant_message_id)
            return self.failure_update(context, self.unexpected_failure(context.delivery), ''.join(chunks))

    def finalize(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """核验网页和知识库引用后保存终态，把正文及两类可信来源返回 State。"""
        context = runtime.context
        failure = state['failure']
        if failure:
            self.execution.search.fail(context.operation, failure)
        answer, web_sources = self.execution.search.resolve(context.operation, state['answer'])
        answer, knowledge_sources = self.execution.resolve_knowledge_citations(state['messages'], answer)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE messages SET content=%s,status=%s,error=%s,web_sources=%s,knowledge_sources=%s,progress=NULL "
                "WHERE id=%s AND status='generating'",
                (answer, 'failed' if failure else 'complete', failure, Jsonb(web_sources),
                 Jsonb(knowledge_sources), context.assistant_message_id),
            )
            connection.execute(
                "UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'",
                (state['reserved_call_id'],),
            )
        LOGGER.info('event=node finalize message_id=%s status=%s characters=%s', context.assistant_message_id, 'failed' if failure else 'complete', len(answer))
        return {'answer': answer, 'web_sources': web_sources, 'knowledge_sources': knowledge_sources,
                'messages': self.answered_messages(state['messages'], answer)}

    @staticmethod
    def answered_messages(messages: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
        """更新最后一条最终助手消息，或追加回答，保持完整快照而不原地修改已有 State。"""
        if messages and messages[-1]['role'] == 'assistant' and not messages[-1].get('tool_calls'):
            return [*messages[:-1], {**messages[-1], 'content': answer}]
        return [*messages, {'role': 'assistant', 'content': answer}]

    @staticmethod
    def failure_update(context: ConversationContext, detail: str, partial: str = '') -> dict[str, Any]:
        """同步失败返回错误说明，流式失败保留已输出正文，均通过 State 更新传递。"""
        return {'failure': detail, 'answer': detail if context.delivery == 'sync' else partial}

    def unexpected_failure(self, delivery: Literal['sync', 'stream']) -> str:
        """按传输方式返回不泄露内部异常的稳定失败说明。"""
        return '回答执行中断，已保留部分内容，可以重试。' if delivery == 'stream' else '记忆操作未完成，可以重试核对。'

    def fail_terminal(self, context: ConversationContext, state: ConversationState, detail: str) -> ConversationResult:
        """图边界异常时使用最新 State 保存失败终态，保留部分回答并撤销预留调用。"""
        answer = state['answer'] or (detail if context.delivery == 'sync' else '')
        try:
            self.execution.search.fail(context.operation, detail)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE messages SET content=%s,status='failed',error=%s WHERE id=%s AND status='generating'",
                    (answer, detail, context.assistant_message_id),
                )
                connection.execute(
                    "UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'",
                    (state['reserved_call_id'],),
                )
        except Exception:
            # 数据库暂时不可用时，由既有启动恢复流程处理未结束消息。
            LOGGER.exception('回答失败终态暂未保存 message_id=%s', context.assistant_message_id)
        return {'answer': answer, 'failure': detail}
