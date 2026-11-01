"""用一张 LangGraph 编排回答准备、模型工具循环和最终提交。"""

from dataclasses import dataclass
import logging
from collections.abc import Callable
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from general_agent.agent.state import AgentState
from general_agent.conversation.execution import ChatExecution
from general_agent.conversation.progress import DRAFTING_PROGRESS, MessageProgress, update_message_progress
from general_agent.infrastructure.checkpoints import Checkpoints, ExecutionPaused, STATE_VERSION
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.infrastructure.settings import StoredSettings

LOGGER = logging.getLogger(__name__)


class ConversationState(AgentState):
    """提供与共享智能体循环兼容的会话级可恢复状态。"""


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
    """通过共享循环图推进回答，业务持久化独立于运行中的可恢复状态。"""

    def __init__(self, database: Database, execution: ChatExecution, checkpoints: Checkpoints | None = None) -> None:
        """绑定业务存储、共享执行器和会话级 PostgreSQL 检查点。"""
        self.database = database
        self.execution = execution
        self.checkpoints = checkpoints

    def run(self, context: ConversationContext, rows: list[dict[str, Any]]) -> ConversationResult:
        """建立本轮初始增量；同一会话已有检查点时只合入当前用户消息。"""
        current = next((row for row in rows if str(row.get('id')) == str(context.user_message_id)), None)
        if current is None:
            raise ValueError('本轮用户消息不存在。')
        source_snapshot = self.execution.agent_runtime.repository.snapshot(context.conversation_id)
        previous = self.checkpoints.values(str(context.conversation_id)) if self.checkpoints else {}
        previous_answer = str(previous.get('answer_id') or '')
        if (self.checkpoints and previous_answer
                and previous_answer != str(context.assistant_message_id)
                and previous.get('settled') is not True):
            # 新尝试不能继承失败、停止或待收尾分支；从已提交业务历史建立干净基线。
            self.checkpoints.discard_answer(context.conversation_id, UUID(previous_answer))
            previous = {}
        usable = bool(previous and previous.get('version') == STATE_VERSION)
        turn_rows = [{**current, '_current_request': True}] if usable else [dict(row) for row in rows]
        if usable:
            # 业务终态可能先于最终检查点提交；用最近一条已提交回答补齐该确定性故障窗口。
            with self.database.connect() as connection:
                committed = connection.execute(
                    "SELECT id,position,role,content FROM messages WHERE conversation_id=%s "
                    "AND role='assistant' AND status='complete' AND position<%s "
                    "ORDER BY position DESC LIMIT 1",
                    (context.conversation_id, int(current.get('position', 0))),
                ).fetchone()
            known = {str(message.get('_message_id')) for message in previous.get('messages', []) if message.get('_message_id')}
            already_present = bool(committed and (
                str(committed['id']) in known
                or any(message.get('role') == 'assistant' and message.get('content') == committed['content']
                       for message in previous.get('messages', []))
            ))
            if committed and not already_present:
                turn_rows.insert(0, {
                    'role': 'assistant', 'content': str(committed['content']),
                    '_message_id': str(committed['id']), '_model_message': True,
                })
        initial: ConversationState = {
            'version': STATE_VERSION,
            'answer_id': str(context.assistant_message_id),
            'messages': [],
            'used_skills': [],
            'tool_calls': [],
            'round_index': 0,
            'tool_failures': {},
            'document_calls': {},
            'source_snapshot': source_snapshot,
            'source_attachment_ids': [],
            'answer': '',
            'failure': None,
            'used_tools': False,
            'inbox_deliveries': [],
            'watching_inbox': False,
            'settled': False,
            'knowledge_source_candidates': [],
            'web_sources': [],
            'knowledge_sources': [],
        }
        return self.invoke(initial, context, turn_rows)

    def invoke(self, graph_input: ConversationState | None, context: ConversationContext,
               messages: list[dict[str, Any]] | None = None) -> ConversationResult:
        """推进或续接单张回答图，并从同一 State 返回最终结果。"""
        LOGGER.info('event=graph_start conversation_id=%s message_id=%s delivery=%s resume=%s',
                    context.conversation_id, context.assistant_message_id, context.delivery, graph_input is None)
        state = graph_input or cast(ConversationState, self.checkpoints.values(str(context.conversation_id)) if self.checkpoints else {})
        try:
            draft = {'written': False, 'progress': False}

            def prepare_turn(current: ConversationState) -> dict[str, Any]:
                """把本轮材料刷新接到共享循环的首个业务节点。"""
                return self.prepare_context(current, context)

            def finalize_turn(current: ConversationState) -> dict[str, Any]:
                """把可交付的循环结果交给统一业务收尾。"""
                return self.finalize(current, context)

            def handle_model_failure(error: Exception, partial: str) -> dict[str, Any]:
                """按传输方式保存模型或准备错误，并保留已经输出的正文。"""
                return self.failure_update(context, str(error), partial)

            state = cast(ConversationState, self.execution.run_answer(
                context.conversation_id,
                context.account_id,
                context.user_message_id,
                context.operation,
                context.settings,
                messages=cast(list[ModelMessage] | None, messages),
                assistant_message_id=context.assistant_message_id,
                on_content=self.content_writer(context, draft),
                on_progress=self.progress_writer(context),
                on_round_start=self.round_starter(context, draft),
                prepare_turn=prepare_turn,
                finalize_turn=finalize_turn,
                model_failure=handle_model_failure,
                initial_state=dict(graph_input or {}),
            ))
            LOGGER.info('event=graph_end message_id=%s failure=%s', context.assistant_message_id, bool(state.get('failure')))
            self.discard_deleted_memory_checkpoint(context.conversation_id, context.user_message_id, context.assistant_message_id)
            return {'answer': str(state.get('answer') or ''), 'failure': state.get('failure')}
        except ExecutionPaused:
            return {'answer': str(state.get('answer') or ''), 'failure': None}
        except Exception:
            LOGGER.exception('LangGraph 对话执行失败 conversation_id=%s message_id=%s',
                             context.conversation_id, context.assistant_message_id)
            result = self.fail_terminal(context, state, self.unexpected_failure(context.delivery))
            self.discard_deleted_memory_checkpoint(context.conversation_id, context.user_message_id, context.assistant_message_id)
            return result

    def content_writer(self, context: ConversationContext, draft: dict[str, bool]) -> Callable[[str], None]:
        """写入流式公开正文；同步入口只依赖 State 中的完整回答。"""

        def on_content(text: str) -> None:
            """追加可见模型分片，状态只保留流式进度而不持有连接。"""
            if not text:
                return
            draft['written'] = True
            if context.delivery != 'stream':
                return
            if not draft['progress']:
                update_message_progress(self.database, context.assistant_message_id, DRAFTING_PROGRESS)
                draft['progress'] = True
            with self.database.connect() as connection:
                connection.execute("UPDATE messages SET content=content || %s WHERE id=%s AND status='generating'",
                                   (text, context.assistant_message_id))
            return

        return on_content

    def progress_writer(self, context: ConversationContext) -> Callable[[MessageProgress], None]:
        """仅流式入口保存最新安全进度，工具参数和推理不进入临时状态。"""
        def on_progress(progress: MessageProgress) -> None:
            """把当前工具阶段投影到回答消息。"""
            if context.delivery == 'stream':
                update_message_progress(self.database, context.assistant_message_id, progress)

        return on_progress

    def round_starter(self, context: ConversationContext, draft: dict[str, bool]) -> Callable[[], None]:
        """模型新回合开始时清空上一轮候选草稿，纯工具回合不增加数据库写入。"""

        def reset_draft() -> None:
            """在确有旧草稿时清空可见正文并重置本轮缓冲。"""
            if draft['written'] and context.delivery == 'stream':
                with self.database.connect() as connection:
                    connection.execute("UPDATE messages SET content='' WHERE id=%s AND status='generating'",
                                       (context.assistant_message_id,))
            draft['written'] = False
            draft['progress'] = False

        return reset_draft

    def prepare_context(self, state: ConversationState, context: ConversationContext) -> dict[str, Any]:
        """初始化本轮材料；checkpoint 的 State 恢复由 LangGraph 完成。"""
        try:
            preparation = self.execution.prepare_response(
                context.conversation_id,
                context.account_id,
                context.user_message_id,
                context.operation,
                state['messages'],
            )
            LOGGER.info('event=node prepare_context message_id=%s messages=%s prepared=%s',
                        context.assistant_message_id, len(state['messages']), len(preparation.messages))
            return {
                'messages': {'replace': preparation.messages},
                'source_attachment_ids': [str(identifier) for identifier in self.execution.material_attachment_ids(preparation.material)],
                'source_snapshot': self.execution.agent_runtime.repository.snapshot(context.conversation_id),
            }
        except (ValueError, ModelFailure) as error:
            return {'failure': str(error), 'answer': str(error) if context.delivery == 'sync' else ''}
        except Exception:
            LOGGER.exception('LangGraph 上下文准备失败 conversation_id=%s message_id=%s',
                             context.conversation_id, context.assistant_message_id)
            return {'failure': self.unexpected_failure(context.delivery),
                    'answer': self.unexpected_failure(context.delivery) if context.delivery == 'sync' else ''}

    def finalize(self, state: ConversationState, context: ConversationContext) -> dict[str, Any]:
        """在业务提交前核验引用并幂等保存最终消息，成功后才确认可结束。"""
        failure = state.get('failure')
        if failure:
            self.execution.search.fail(context.operation, failure)
        answer, web_sources = self.execution.search.resolve(context.operation, str(state.get('answer') or ''))
        root_run_id = str(state.get('root_run_id') or '')
        registered_sources = (self.execution.agent_runtime.repository.sources(UUID(root_run_id))
                              if root_run_id else [])
        candidates = [source for source in registered_sources
                      if isinstance(source.get('key'), str) and source['key'].startswith('K')]
        answer, knowledge_sources = self.execution.resolve_knowledge_citations(candidates, answer)
        with self.database.connect() as connection:
            completed = connection.execute(
                "UPDATE messages SET content=%s,status=%s,error=%s,web_sources=%s,knowledge_sources=%s,progress=NULL "
                "WHERE id=%s AND status='generating' RETURNING id",
                (answer, 'failed' if failure else 'complete', failure, Jsonb(web_sources),
                 Jsonb(knowledge_sources), context.assistant_message_id),
            ).fetchone()
        if completed:
            self.execution.close_agent_execution(context.assistant_message_id)
        final_messages = self.answered_messages(list(state['messages']), answer)
        final_messages[-1]['_message_id'] = str(context.assistant_message_id)
        LOGGER.info('event=node finalize message_id=%s status=%s characters=%s',
                    context.assistant_message_id, 'failed' if failure else 'complete', len(answer))
        return {'messages': {'replace': final_messages}, 'answer': answer, 'failure': failure,
                'knowledge_source_candidates': candidates,
                'web_sources': web_sources, 'knowledge_sources': knowledge_sources, 'settled': True}

    @staticmethod
    def answered_messages(messages: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
        """用最终正文更新最后一条候选回答，保持工作消息完整且不重复。"""
        if messages and messages[-1].get('role') == 'assistant' and not messages[-1].get('tool_calls'):
            return [*messages[:-1], {**messages[-1], 'content': answer}]
        return [*messages, {'role': 'assistant', 'content': answer}]

    @staticmethod
    def failure_update(context: ConversationContext, detail: str, partial: str = '') -> dict[str, Any]:
        """同步失败返回错误说明，流式失败保留已经输出的正文。"""
        return {'failure': detail, 'answer': detail if context.delivery == 'sync' else partial}

    def unexpected_failure(self, delivery: Literal['sync', 'stream']) -> str:
        """按传输方式返回不泄露内部异常的稳定失败说明。"""
        return '回答执行中断，已保留部分内容，可以重试。' if delivery == 'stream' else '记忆操作未完成，可以重试核对。'

    def fail_terminal(self, context: ConversationContext, state: ConversationState, detail: str) -> ConversationResult:
        """图边界异常时保存失败终态并尽量保留已有回答。"""
        answer = str(state.get('answer') or (detail if context.delivery == 'sync' else ''))
        try:
            self.execution.search.fail(context.operation, detail)
            with self.database.connect() as connection:
                completed = connection.execute(
                    "UPDATE messages SET content=%s,status='failed',error=%s WHERE id=%s AND status='generating' RETURNING id",
                    (answer, detail, context.assistant_message_id),
                ).fetchone()
            if completed:
                self.execution.close_agent_execution(context.assistant_message_id)
        except Exception:
            LOGGER.exception('回答失败终态暂未保存 message_id=%s', context.assistant_message_id)
        return {'answer': answer, 'failure': detail}

    def discard_deleted_memory_checkpoint(self, conversation_id: UUID, user_message_id: UUID,
                                          assistant_message_id: UUID) -> None:
        """回答进入不可续接终态后清除已停用记忆的派生线程。"""
        try:
            if self.checkpoints and self.execution.memory_deletion_invalidates_checkpoint(user_message_id):
                self.checkpoints.discard_answer(conversation_id, assistant_message_id)
        except Exception:
            LOGGER.exception('event=deleted_memory_checkpoint_cleanup_failed conversation_id=%s message_id=%s',
                             conversation_id, assistant_message_id)
