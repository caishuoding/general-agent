"""运行唯一 Agent 图，并管理主从递归、检查点、暂停和恢复。"""

import logging
import os
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from langgraph.runtime import Runtime

from general_agent.agent.compaction import ContextCompactor
from general_agent.agent.context import AgentContext, AgentRole
from general_agent.agent.graph import build_agent_graph
from general_agent.agent.nodes import AgentNodes
from general_agent.agent.orchestration import AgentOrchestrator
from general_agent.agent.repository import AgentRepository, RunCheckpoint
from general_agent.agent.result import AgentResult
from general_agent.agent.state import (
    STATE_VERSION,
    AgentState,
    merge_agent_messages,
    new_agent_state,
)
from general_agent.chat.progress import MessageProgress
from general_agent.chat.services import ChatServices
from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.infrastructure.checkpoints import Checkpoints, ExecutionPaused
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.settings import StoredSettings
from general_agent.infrastructure.tokens import TokenCounter
from general_agent.prompts.agents import SUBAGENT_PROMPT
from general_agent.skills.catalog import SkillCatalog
from general_agent.skills.runtime import SkillRuntime
from general_agent.skills.service import SkillService

LOGGER = logging.getLogger(__name__)


class AgentRunner:
    """创建运行上下文、编译唯一图并处理新回答、恢复和子智能体执行。"""

    def __init__(self, services: ChatServices, checkpoints: Checkpoints | None = None) -> None:
        """绑定聊天服务、持久化仓库、主从调度和受信任 Skill 运行环境。"""
        self.services = services
        self.checkpoints = checkpoints
        self.repository = AgentRepository(services.memory.database)
        self.orchestrator = AgentOrchestrator(self.repository)
        root = Path(os.environ.get('SKILLS_DIR', str(Path(__file__).resolve().parents[3] / 'skills')))
        catalog = SkillCatalog(root)
        self.skill_runtime = SkillRuntime(
            SkillService(services.memory.database, services.memory.directory, catalog),
            services.attachments,
        )

    def run_answer(
        self,
        *,
        conversation_id: UUID,
        account_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        settings: StoredSettings,
        operation: OperationContext,
        delivery: Literal['sync', 'stream'],
        on_content: Callable[[str], None] | None = None,
        on_progress: Callable[[MessageProgress], None] | None = None,
        on_round_start: Callable[[], None] | None = None,
    ) -> AgentResult:
        """开始一次主智能体回答，并在非暂停终态关闭根执行。"""
        root = self.repository.start(assistant_message_id, account_id, conversation_id, operation)
        context = AgentContext(
            role=AgentRole.MAIN,
            conversation_id=conversation_id,
            account_id=account_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            run_id=UUID(str(root['id'])),
            settings=settings,
            operation=operation,
            delivery=delivery,
            on_content=on_content,
            on_progress=on_progress,
            on_round_start=on_round_start,
        )
        initial = self._new_turn_state(context)
        try:
            result, _ = self._execute(context, initial)
            return result
        finally:
            self._close_when_finished(context)

    def continue_answer(
        self,
        *,
        conversation_id: UUID,
        account_id: UUID,
        user_message_id: UUID,
        assistant_message_id: UUID,
        settings: StoredSettings,
        operation: OperationContext,
        delivery: Literal['sync', 'stream'],
        has_checkpoint: bool,
        on_content: Callable[[str], None] | None = None,
        on_progress: Callable[[MessageProgress], None] | None = None,
        on_round_start: Callable[[], None] | None = None,
    ) -> AgentResult:
        """从原检查点或首次节点前状态继续主回答，不创建新的消息身份。"""
        root = self.repository.start(assistant_message_id, account_id, conversation_id, operation)
        context = AgentContext(
            role=AgentRole.MAIN,
            conversation_id=conversation_id,
            account_id=account_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            run_id=UUID(str(root['id'])),
            settings=settings,
            operation=operation,
            delivery=delivery,
            on_content=on_content,
            on_progress=on_progress,
            on_round_start=on_round_start,
        )
        initial = self._new_turn_state(context) if not has_checkpoint else None
        try:
            result, _ = self._execute(context, initial)
            return result
        finally:
            self._close_when_finished(context)

    def resume_operation(self, connection: Any, message: dict[str, Any], account_id: UUID,
                         settings: StoredSettings) -> OperationContext:
        """认领原中断回答并补偿暂停时长，不创建新的用户或助手消息。"""
        answer_id, conversation_id = message['id'], message['conversation_id']
        if message['resume_model_revision'] != settings.revision:
            raise ValueError('模型配置已变化，请重试创建新的回答。')
        root = connection.execute(
            'SELECT * FROM agents WHERE answer_attempt_id=%s AND parent_agent_id IS NULL',
            (answer_id,),
        ).fetchone()
        if root and root['source_snapshot'] != self.repository.snapshot(conversation_id):
            raise ValueError('资料范围已变化，请重新提问。')
        if root and root['deadline'] <= message['interrupted_at']:
            raise ValueError('原执行在中断前已超时，请重试。')
        operation = connection.execute(
            'SELECT * FROM cloud_operations WHERE id=%s', (message['operation_id'],)
        ).fetchone()
        if not operation or operation['cancelled'] or operation['invalidated']:
            raise ValueError('原操作已停止或来源已失效，请重新提问。')
        connection.execute(
            'UPDATE agents SET deadline=deadline+(CURRENT_TIMESTAMP-%s),closed=FALSE '
            'WHERE answer_attempt_id=%s', (message['interrupted_at'], answer_id))
        connection.execute(
            "UPDATE agent_runs r SET deadline=r.deadline+(CURRENT_TIMESTAMP-%s),paused_at=NULL,"
            "status=CASE WHEN a.parent_agent_id IS NOT NULL THEN 'queued' ELSE 'running' END "
            'FROM agents a WHERE r.agent_id=a.id AND a.answer_attempt_id=%s '
            "AND (a.parent_agent_id IS NULL OR r.status IN ('queued','running'))",
            (message['interrupted_at'], answer_id),
        )
        return OperationContext(operation['id'], operation['purpose'], conversation_id, account_id)

    def heartbeat(self) -> None:
        """记录进程仍实际执行的时间，离线和等待续接不消耗剩余执行时长。"""
        with self.repository.database.connect() as connection:
            connection.execute(
                'UPDATE messages SET execution_heartbeat=CURRENT_TIMESTAMP '
                "WHERE status='generating' AND resume_model_revision IS NOT NULL"
            )

    def _execute(self, context: AgentContext, initial: AgentState | None) -> tuple[AgentResult, AgentState]:
        """编译并执行唯一 Agent 图，主从只通过 AgentContext 角色区分。"""
        run_child = self._child_runner(context)
        if context.is_main:
            self._resume_pending_children(context, run_child)
        counter = TokenCounter.for_settings(context.settings, self.services.memory.directory)
        compactor = ContextCompactor(
            counter, context.settings, self.services.memory.calls.bind(context.operation),
            context.assistant_message_id,
        )
        skill_runtime = self.skill_runtime if context.is_main else None
        checkpoint = RunCheckpoint(self.repository, context.run_id, context.user_message_id)
        nodes = AgentNodes(
            services=self.services,
            checkpoint=checkpoint,
            repository=self.repository,
            orchestrator=self.orchestrator,
            compactor=compactor,
            counter=counter,
            skill_runtime=skill_runtime,
            run_child=run_child,
        )
        execution_lock = (
            self.checkpoints.execution(context.conversation_id, wait=True)
            if self.checkpoints and context.is_main else nullcontext()
        )
        saver_context = (
            self.checkpoints.saver(context.conversation_id, context.assistant_message_id, context.thread_id)
            if self.checkpoints else nullcontext(None)
        )
        if self.checkpoints:
            self.checkpoints.register(context.thread_id, context.conversation_id, context.assistant_message_id)
        with execution_lock, saver_context as saver:
            graph = build_agent_graph(nodes, saver)
            recursion_rounds = LIMITS.skill_rounds if context.is_main else LIMITS.child_rounds
            config: Any = {
                'configurable': {'thread_id': context.thread_id},
                'recursion_limit': recursion_rounds * 4 + 8,
            }
            try:
                if self.checkpoints and initial is None:
                    saved = graph.get_state(config)
                    if saved and saved.values:
                        if saved.values.get('version') != STATE_VERSION:
                            raise ValueError('Agent 检查点版本不兼容，请重试。')
                        output = graph.invoke(None, config, context=context, durability='sync')
                    else:
                        output = graph.invoke(
                            self._new_turn_state(context), config, context=context, durability='sync')
                elif initial is not None:
                    output = graph.invoke(initial, config, context=context,
                                          durability='sync' if self.checkpoints else None)
                else:
                    output = graph.invoke(None, config, context=context)
            except Exception:
                LOGGER.exception('Agent 图执行失败 run_id=%s', context.run_id)
                if not context.is_main:
                    raise
                self.repository.guard(context.run_id)
                latest = graph.get_state(config).values if self.checkpoints else initial
                failed = cast(AgentState, latest or new_agent_state(run_id=str(context.run_id)))
                with self.repository.database.connect() as connection:
                    message = connection.execute(
                        'SELECT content FROM messages WHERE id=%s',
                        (context.assistant_message_id,),
                    ).fetchone()
                partial = str(message['content'] or '') if message else ''
                update = nodes._failure_update(
                    failed, context, RuntimeError('回答执行中断，已保留部分内容，可以重试。'), partial,
                )
                failed = cast(AgentState, {
                    **failed, **update,
                    'messages': merge_agent_messages(failed['messages'], update.get('messages', [])),
                })
                terminal = nodes.finalize(failed, Runtime(context=context))
                output = {**failed, **terminal, 'messages': merge_agent_messages(
                    failed['messages'], terminal.get('messages', []))}
        state = cast(AgentState, output)
        return self._result_from_state(state, context), state

    def _resume_pending_children(self, context: AgentContext,
                                 run_child: Callable[[UUID], None]) -> None:
        """把重启后仍排队的子任务重新提交工作池，保持原 Run 身份。"""
        info = self.repository.info(context.run_id)
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                "SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id "
                "WHERE a.parent_agent_id=%s AND r.status='queued' ORDER BY r.sequence",
                (info['agent_id'],),
            ).fetchall()
        for row in rows:
            child_id = UUID(str(row['id']))

            def resume_child(identifier: UUID = child_id) -> None:
                """以原执行标识重新提交恢复后的子任务。"""
                run_child(identifier)

            self.orchestrator.submit(child_id, resume_child)

    def _child_runner(self, parent_context: AgentContext) -> Callable[[UUID], None]:
        """创建子任务执行回调，子智能体复用同一图但无 Skill 和编排权限。"""
        def run_child(child_run_id: UUID) -> None:
            """执行一个子任务 Run，并把真实结果或错误写入业务状态。"""
            info = self.repository.info(child_run_id)
            history = [dict(item) for item in info['messages'] if item.get('role') != 'system']
            history.append({'id': f"task:{child_run_id}", 'role': 'user', 'content': info['task']})
            if not info['messages']:
                material = self.services.attachments.prepare(
                    conversation_id=parent_context.conversation_id,
                    account_id=parent_context.account_id,
                )
                history = [*[dict(message) for message in material.messages], *history]
            instructions = str(info.get('instructions') or '').strip()
            fragments = [SUBAGENT_PROMPT]
            if instructions:
                fragments.append('本轮补充要求：\n' + instructions)
            child_context = AgentContext(
                role=AgentRole.SUBAGENT,
                conversation_id=UUID(str(info['conversation_id'])),
                account_id=UUID(str(info['account_id'])),
                user_message_id=parent_context.user_message_id,
                assistant_message_id=UUID(str(info['answer_attempt_id'])),
                run_id=child_run_id,
                settings=parent_context.settings,
                operation=RunCheckpoint(self.repository, child_run_id, parent_context.user_message_id).operation,
                delivery=parent_context.delivery,
                context_fragments=tuple(fragments),
            )
            initial = new_agent_state(run_id=str(child_run_id), messages=history)
            try:
                saved = self.checkpoints.values(child_context.thread_id) if self.checkpoints else {}
                result, final_state = self._execute(child_context, None if saved else initial)
                self.repository.guard(child_run_id)
                clean = self._clean_messages(list(final_state['messages']))
                if result.error:
                    LOGGER.info('event=child_failed run_id=%s error=%s', child_run_id, result.error)
                    self.repository.transition(child_run_id, 'failed', error=result.error)
                else:
                    LOGGER.info('event=child_completed run_id=%s output=%s', child_run_id, bool(result.output))
                    self.repository.transition(child_run_id, 'completed', result.output, clean)
            except ExecutionPaused:
                return
            except Exception as error:
                detail = str(error) if isinstance(error, (ValueError, LookupError)) else '子任务未完成，已保留真实调用用量。'
                self.repository.transition(child_run_id, 'failed', error=detail)
                raise
        return run_child

    def _new_turn_state(self, context: AgentContext) -> AgentState:
        """建立主回答本轮增量；旧未完成回答不复用于新回答身份。"""
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                'SELECT id,position,role,content,skill_names FROM messages '
                "WHERE conversation_id=%s AND status='complete' ORDER BY position",
                (context.conversation_id,),
            ).fetchall()
            owner = connection.execute(
                'SELECT answer_id FROM checkpoint_threads WHERE thread_id=%s',
                (context.thread_id,),
            ).fetchone()
        current = next((row for row in rows if str(row.get('id')) == str(context.user_message_id)), None)
        if current is None:
            raise ValueError('本轮用户消息不存在。')
        previous = self.checkpoints.values(context.thread_id) if self.checkpoints else {}
        previous_answer = str(owner['answer_id']) if owner else ''
        if (self.checkpoints and previous_answer
                and previous_answer != str(context.assistant_message_id)
                and (previous.get('finished') is not True or previous.get('error'))):
            self.checkpoints.discard_answer(context.conversation_id, UUID(previous_answer))
            previous = {}
        usable = bool(previous and previous.get('version') == STATE_VERSION)
        turn_rows = [{**current, '_current_request': True}] if usable else [dict(row) for row in rows]
        if usable:
            with self.repository.database.connect() as connection:
                committed = connection.execute(
                    "SELECT id,position,role,content FROM messages WHERE conversation_id=%s "
                    "AND role='assistant' AND status='complete' AND position<%s "
                    'ORDER BY position DESC LIMIT 1',
                    (context.conversation_id, int(current.get('position', 0))),
                ).fetchone()
            known = {str(message.get('id')) for message in previous.get('messages', []) if message.get('id')}
            already_present = bool(committed and (
                str(committed['id']) in known
                or any(message.get('role') == 'assistant' and message.get('content') == committed['content']
                       for message in previous.get('messages', []))
            ))
            if committed and not already_present:
                turn_rows.insert(0, {
                    'id': str(committed['id']),
                    'role': 'assistant',
                    'content': str(committed['content']),
                    '_model_message': True,
                })
        return new_agent_state(run_id=str(context.run_id), messages=turn_rows)

    @staticmethod
    def _result_from_state(state: AgentState, context: AgentContext) -> AgentResult:
        """从唯一 State 提取最终 assistant 文本和致命错误。"""
        output: str | None = None
        for message in reversed(state.get('messages', [])):
            identifier = str(message.get('id') or '')
            if identifier != str(context.assistant_message_id) and not identifier.startswith(f'assistant:{context.run_id}:'):
                continue
            if message.get('role') == 'assistant' and not message.get('tool_calls'):
                output = str(message.get('content') or '')
                break
        if output is None and state.get('error') and context.delivery == 'sync':
            output = str(state['error'])
        return AgentResult(output=output, error=state.get('error'))

    @staticmethod
    def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """保存子任务消息时移除私有推理，不改变工具和公开正文。"""
        return [{key: value for key, value in message.items() if key != 'reasoning_content'}
                for message in messages]

    def _close_when_finished(self, context: AgentContext) -> None:
        """非暂停终态清理失效记忆检查点并关闭根 Agent。"""
        paused = False
        if self.checkpoints is not None:
            with self.repository.database.connect() as connection:
                paused = bool(connection.execute(
                    "SELECT id FROM messages WHERE id=%s AND status='interrupted'",
                    (context.assistant_message_id,)
                ).fetchone())
        if not paused:
            try:
                if (self.checkpoints and context.is_main
                        and self.services.memory_deletion_invalidates_checkpoint(context.user_message_id)):
                    self.checkpoints.discard_answer(
                        context.conversation_id, context.assistant_message_id)
            finally:
                self.repository.close(context.assistant_message_id)
