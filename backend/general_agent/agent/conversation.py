"""用 LangGraph State 统一传递同步与流式回答的执行数据。"""

from dataclasses import dataclass
from contextlib import nullcontext
import logging
from typing import Any, Literal, TypedDict, cast, NotRequired
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from psycopg.types.json import Jsonb

from general_agent.conversation.execution import ChatExecution
from general_agent.conversation.progress import DRAFTING_PROGRESS, MessageProgress, update_message_progress
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.checkpoints import Checkpoints, ExecutionPaused, STATE_VERSION
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.infrastructure.settings import StoredSettings

LOGGER = logging.getLogger(__name__)


class ConversationState(TypedDict):
    """共享本次回答的完整消息快照、材料来源、回答和错误，不携带凭据或连接。"""

    version: NotRequired[int]
    answer_id: NotRequired[str]
    history_position: NotRequired[int]
    source_snapshot: NotRequired[dict[str, Any]]
    runtime_state: NotRequired[dict[str, Any]]
    messages: list[dict[str, Any]]
    source_attachment_ids: list[str]
    answer: str
    failure: str | None
    used_tools: bool
    knowledge_source_candidates: list[dict[str, Any]]
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

    def __init__(self, database: Database, execution: ChatExecution, checkpoints: Checkpoints | None = None) -> None:
        """保存图定义，生产执行绑定 PostgreSQL saver，消息按完整快照覆盖。"""
        self.database = database
        self.execution = execution
        self.checkpoints = checkpoints
        builder = StateGraph(ConversationState, context_schema=ConversationContext)
        builder.add_node('prepare_context', self.prepare_context)
        builder.add_node('agent', self.agent)
        builder.add_node('finalize', self.finalize)
        builder.add_edge(START, 'prepare_context')
        builder.add_edge('prepare_context', 'agent')
        builder.add_edge('agent', 'finalize')
        builder.add_edge('finalize', END)
        self.builder = builder
        self.compiled = builder.compile(name='conversation')

    def run(self, context: ConversationContext, rows: list[dict[str, Any]]) -> ConversationResult:
        """对齐已提交回答水位后恢复工作消息，仅合入新消息并按稳定身份去重。"""
        position = max((int(row.get("position", 0)) for row in rows), default=0)
        source_snapshot: dict[str, Any] = {}
        inherited_runtime: dict[str, Any] = {}
        if self.checkpoints is not None:
            source_snapshot = self.execution.agent_runtime.repository.snapshot(context.conversation_id)
            previous = self.checkpoints.values(str(context.conversation_id))
            if previous.get('answer_id'):
                with self.database.connect() as connection:
                    committed = connection.execute('SELECT id,position,content,status FROM messages WHERE id=%s AND conversation_id=%s', (UUID(previous['answer_id']), context.conversation_id)).fetchone()
                if committed and committed['status'] == 'complete':
                    previous['history_position'] = max(previous.get('history_position', 0), committed['position'])
                    previous['messages'] = self.answered_messages(previous.get('messages', []), committed['content'])
                    previous['messages'][-1]['_message_id'] = str(committed['id'])
                else:
                    previous = {}
            if (previous.get("version") == STATE_VERSION and not previous.get("failure")
                    and previous.get("source_snapshot") == source_snapshot
                    and previous.get("history_position", 0) < position):
                inherited_runtime = previous.get('runtime_state', {})
                recent = [{**message, "_model_message": True} for message in previous.get("messages", [])
                          if message.get("role") != "system" and not message.get('_context_material')]
                known = {message.get('_message_id') for message in recent if message.get('_message_id')}
                rows = [*recent, *(row for row in rows if int(row.get('position', 0)) > previous.get('history_position', 0)
                    and str(row.get('id')) not in known)]
        initial: ConversationState = {
            'version': STATE_VERSION, 'answer_id': str(context.assistant_message_id),
            'history_position': position, 'source_snapshot': source_snapshot, 'runtime_state': inherited_runtime,
            'messages': [dict(row) for row in rows],
            'source_attachment_ids': [],
            'answer': '',
            'failure': None,
            'used_tools': False,
            'knowledge_source_candidates': [],
            'web_sources': [],
            'knowledge_sources': [],
        }
        return self.invoke(initial, context)

    def invoke(self, graph_input: ConversationState | None, context: ConversationContext) -> ConversationResult:
        """短暂认领回答归属后推进图；空输入续接，旧回答不能覆盖新检查点。"""
        LOGGER.info('event=graph_start conversation_id=%s message_id=%s delivery=%s', context.conversation_id, context.assistant_message_id, context.delivery)
        state = graph_input or cast(ConversationState, self.checkpoints.values(str(context.conversation_id)) if self.checkpoints else {})
        try:
            if self.checkpoints:
                with self.checkpoints.conversation(context.conversation_id, wait=True):
                    self.checkpoints.register(str(context.conversation_id), context.conversation_id, context.assistant_message_id)
            with (self.checkpoints.saver(context.conversation_id, context.assistant_message_id) if self.checkpoints else nullcontext(None)) as saver:
                compiled = self.builder.compile(checkpointer=saver, name="conversation") if saver else self.compiled
                for snapshot in compiled.stream(
                    graph_input,
                    {
                        'configurable': {'thread_id': str(context.conversation_id)},
                        'metadata': {'conversation_id': str(context.conversation_id),
                                     'assistant_message_id': str(context.assistant_message_id),
                                     'session_id': str(context.conversation_id)},
                    },
                    context=context,
                    stream_mode='values', durability='sync' if saver else None,
                ):
                    state = cast(ConversationState, snapshot)
            LOGGER.info('event=graph_end message_id=%s failure=%s', context.assistant_message_id, bool(state['failure']))
            return {'answer': state['answer'], 'failure': state['failure']}
        except ExecutionPaused:
            return {"answer": state.get("answer", ""), "failure": None}
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
                state['messages'],
            )
            LOGGER.info('event=node prepare_context message_id=%s', context.assistant_message_id)
            return {
                'messages': preparation.messages,
                'source_attachment_ids': [str(identifier) for identifier in self.execution.material_attachment_ids(preparation.material)],
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
        draft_written = bool(self.checkpoints)

        def on_progress(progress: MessageProgress) -> None:
            """只在流式入口写入最新工具状态，同步调用不需要临时进度。"""
            if context.delivery == 'stream':
                update_message_progress(self.database, context.assistant_message_id, progress)

        def reset_draft() -> None:
            """新轮次仅在上轮写过草稿时清空正文，纯工具轮不增加数据库写入。"""
            nonlocal progress_started, draft_written
            chunks.clear()
            progress_started = False
            if draft_written and context.delivery == 'stream':
                with self.database.connect() as connection:
                    connection.execute("UPDATE messages SET content='' WHERE id=%s AND status='generating'", (context.assistant_message_id,))
            draft_written = False

        def on_content(text: str) -> None:
            """缓冲本轮模型文字并更新流式正文，完成后由 State 交给最终收尾。"""
            nonlocal progress_started, draft_written
            if not text:
                return
            chunks.append(text)
            draft_written = True
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
                context.settings,
                messages=cast(list[ModelMessage], state['messages']),
                assistant_message_id=context.assistant_message_id,
                on_content=on_content,
                on_progress=on_progress,
                on_round_start=reset_draft,
                source_attachment_ids=[UUID(identifier) for identifier in state['source_attachment_ids']],
                **({'runtime_state': state.get('runtime_state', {})} if self.checkpoints else {}),
            )
            answer = outcome.answer or ''.join(chunks)
            if not answer:
                raise ValueError('模型没有返回最终回答。')
            LOGGER.info('event=node agent message_id=%s used_tools=%s characters=%s', context.assistant_message_id, outcome.used_tools, len(answer))
            return {'runtime_state': outcome.runtime_state, 'answer': answer, 'used_tools': outcome.used_tools,
                    'knowledge_source_candidates': outcome.knowledge_source_candidates,
                    'messages': outcome.messages if outcome.messages is not None else self.answered_messages(state['messages'], answer)}
        except (ValueError, ModelFailure) as error:
            return self.failure_update(context, str(error), ''.join(chunks))
        except Exception:
            LOGGER.exception('LangGraph 工具节点失败 conversation_id=%s message_id=%s', context.conversation_id, context.assistant_message_id)
            return self.failure_update(context, self.unexpected_failure(context.delivery), ''.join(chunks))

    @staticmethod
    def settle_skill_task(connection: Any, state: ConversationState,
                          context: ConversationContext, status: str) -> None:
        """将当前活动 Skill 任务与回答终态同步，失败和停止标记为可续接中断。"""
        task_id = state.get('runtime_state', {}).get('task_id')
        if not task_id:
            saved = connection.execute(
                "SELECT s.payload FROM agent_messages s JOIN agent_runs r ON r.id=s.run_id "
                "JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=%s AND "
                "a.parent_agent_id IS NULL AND s.step_key='runtime:skill'",
                (context.assistant_message_id,),
            ).fetchone()
            payload = saved.get('payload') if isinstance(saved, dict) else None
            candidate = payload.get('task_id') if isinstance(payload, dict) else None
            task_id = candidate if isinstance(candidate, str) else None
        if not task_id:
            return
        connection.execute(
            "UPDATE skill_tasks SET status=%s,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=%s AND account_id=%s AND conversation_id=%s AND status='active'",
            (status, UUID(task_id), context.account_id, context.conversation_id),
        )

    def finalize(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """核验网页和知识库引用后保存终态，把正文及两类可信来源返回 State。"""
        context = runtime.context
        failure = state['failure']
        if failure:
            self.execution.search.fail(context.operation, failure)
        answer, web_sources = self.execution.search.resolve(context.operation, state['answer'])
        answer, knowledge_sources = self.execution.resolve_knowledge_citations(state['knowledge_source_candidates'], answer)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE messages SET content=%s,status=%s,error=%s,web_sources=%s,knowledge_sources=%s,progress=NULL "
                "WHERE id=%s AND status='generating'",
                (answer, 'failed' if failure else 'complete', failure, Jsonb(web_sources),
                 Jsonb(knowledge_sources), context.assistant_message_id),
            )
            self.settle_skill_task(connection, state, context, 'interrupted' if failure else 'waiting')
        LOGGER.info('event=node finalize message_id=%s status=%s characters=%s', context.assistant_message_id, 'failed' if failure else 'complete', len(answer))
        with self.database.connect() as connection:
            saved = connection.execute('SELECT position FROM messages WHERE id=%s', (context.assistant_message_id,)).fetchone()
        final_messages = self.answered_messages(state['messages'], answer)
        final_messages[-1]['_message_id'] = str(context.assistant_message_id)
        return {'history_position': int(saved['position']) if saved and self.checkpoints else state.get('history_position', 0),
                'answer': answer, 'web_sources': web_sources, 'knowledge_sources': knowledge_sources,
                'messages': final_messages}

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
        """图边界异常时使用最新 State 保存失败终态并保留部分回答。"""
        answer = state['answer'] or (detail if context.delivery == 'sync' else '')
        try:
            self.execution.search.fail(context.operation, detail)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE messages SET content=%s,status='failed',error=%s WHERE id=%s AND status='generating'",
                    (answer, detail, context.assistant_message_id),
                )
                self.settle_skill_task(connection, state, context, 'interrupted')
        except Exception:
            # 数据库暂时不可用时，由既有启动恢复流程处理未结束消息。
            LOGGER.exception('回答失败终态暂未保存 message_id=%s', context.assistant_message_id)
        return {'answer': answer, 'failure': detail}
