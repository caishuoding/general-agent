"""复用工具图执行主从智能体，隔离消息、证据和生命周期。"""

from contextlib import nullcontext
import json
import httpx
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING, cast
from collections.abc import Callable
from uuid import UUID

from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.infrastructure.checkpoints import Checkpoints, ExecutionPaused
from general_agent.agent.graph import AgentToolGraph
from general_agent.agent.compaction import ContextCompactor
from general_agent.agent.message_protocol import protocol_messages
from general_agent.agent.orchestration import AgentOrchestrator
from general_agent.agent.repository import AgentRepository, RunCheckpoint
from general_agent.agent.tools import ORCHESTRATION, WORKER_TOOLS, orchestration_tools
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelMessage, ModelFailure
from general_agent.infrastructure.settings import StoredSettings
from general_agent.infrastructure.tokens import TokenCounter
from general_agent.conversation.progress import (
    CONNECTION_RESUMED_PROGRESS,
    DRAFTING_PROGRESS,
    SUBAGENT_PROGRESS,
    MessageProgress,
    connection_retry_progress,
    tool_progress,
)
from general_agent.memory.automatic import MemoryCheckpoint
from general_agent.prompts.agents import SUBAGENT_PROMPT
from general_agent.skills.catalog import SkillCatalog
from general_agent.skills.runtime import SkillRuntime, SkillSession
from general_agent.skills.service import SkillService
from general_agent.skills.tools import SKILL_TOOLS, skill_tools

if TYPE_CHECKING:
    from general_agent.memory.tools import MemoryTools, ToolOutcome


class AgentRuntime:
    """将一次回答的主从运行集中在同一模块，调用者只需提供固定身份和材料。"""

    def __init__(self, tools: 'MemoryTools') -> None:
        """绑定主从调度与受信任 Skill 目录，命令运行使用独立隔离环境。"""
        self.tools = tools
        self.checkpoints: Checkpoints | None = None
        self.repository = AgentRepository(tools.memory.database)
        self.orchestrator = AgentOrchestrator(self.repository)
        root = Path(os.environ.get('SKILLS_DIR', str(Path(__file__).resolve().parents[3] / 'skills')))
        catalog = SkillCatalog(root)
        self.skills = (SkillRuntime(SkillService(tools.memory.database, tools.memory.directory, catalog), tools.attachments)
                       if tools.attachments is not None else None)

    def run(self, conversation_id: UUID, account_id: UUID, user_id: UUID, answer_id: UUID,
            settings: StoredSettings, operation: OperationContext, messages: list[ModelMessage],
            on_content: Callable[[str], None] | None, attachment_ids: list[UUID] | None,
            on_progress: Callable[[MessageProgress], None] | None = None,
            on_round_start: Callable[[], None] | None = None) -> 'ToolOutcome':
        """主图完成信箱收尾后返回结果，异常出口关闭剩余子任务且保留用量。"""
        from general_agent.memory.tools import ToolOutcome
        root = self.repository.start(answer_id, account_id, conversation_id, operation)
        try:
            result = self.execute(root['id'], user_id, answer_id, settings, cast(list[dict[str, Any]], messages), on_content, attachment_ids, on_progress, on_round_start)
            if self.repository.info(root['id'])['status'] != 'completed':
                resumed = RunCheckpoint(self.repository, root['id'], user_id)
                if not self.orchestrator.inbox.settle(resumed, result['answer'], result['messages']):
                    raise ValueError('主回答尚未完成子任务收尾。')
            # 只把已登记、已交付的知识库来源传给引用核验，不伪造工具消息污染追踪。
            sources = self.repository.sources(root['id'])
            knowledge_sources = [source for source in sources
                                 if isinstance(source.get('key'), str) and source['key'].startswith('K')]
            return ToolOutcome(result['answer'], bool(result['used_tools']), result['messages'], knowledge_sources)
        finally:
            paused = False
            if getattr(self, 'checkpoints', None) is not None:
                with self.repository.database.connect() as connection:
                    paused = bool(connection.execute("SELECT id FROM messages WHERE id=%s AND status='interrupted'", (answer_id,)).fetchone())
            if not paused:
                self.repository.close(answer_id)

    def heartbeat(self) -> None:
        """记录进程仍实际执行的时间，离线和等待续接不消耗剩余执行时长。"""
        with self.repository.database.connect() as connection:
            connection.execute("UPDATE messages SET execution_heartbeat=CURRENT_TIMESTAMP WHERE status='generating' AND resume_model_revision IS NOT NULL")

    def resume(self, connection: Any, message: dict[str, Any], account_id: UUID,
               settings: StoredSettings) -> OperationContext:
        """在认领事务内验证执行材料和配置，恢复原身份并补偿暂停时长。"""
        answer_id, conversation_id = message['id'], message['conversation_id']
        if message['resume_model_revision'] != settings.revision:
            raise ValueError('模型配置已变化，请重试创建新的回答。')
        root = connection.execute('SELECT * FROM agents WHERE answer_attempt_id=%s AND parent_agent_id IS NULL', (answer_id,)).fetchone()
        if root and root['source_snapshot'] != self.repository.snapshot(conversation_id):
            raise ValueError('资料范围已变化，请重新提问。')
        if root and root['deadline'] <= message['interrupted_at']:
            raise ValueError('原执行在中断前已超时，请重试。')
        operation = connection.execute('SELECT * FROM cloud_operations WHERE id=%s', (message['operation_id'],)).fetchone()
        if not operation or operation['cancelled'] or operation['invalidated']:
            raise ValueError('原操作已停止或来源已失效，请重新提问。')
        connection.execute('UPDATE agents SET deadline=deadline+(CURRENT_TIMESTAMP-%s),closed=FALSE WHERE answer_attempt_id=%s', (message['interrupted_at'], answer_id))
        connection.execute("UPDATE agent_runs r SET deadline=r.deadline+(CURRENT_TIMESTAMP-%s),paused_at=NULL,status=CASE WHEN a.parent_agent_id IS NOT NULL THEN 'queued' ELSE 'running' END FROM agents a WHERE r.agent_id=a.id AND a.answer_attempt_id=%s AND (a.parent_agent_id IS NULL OR r.status IN ('queued','running'))", (message['interrupted_at'], answer_id))
        return OperationContext(operation['id'], operation['purpose'], conversation_id, account_id)

    def execute(self, run_id: UUID, user_id: UUID, answer_id: UUID, settings: StoredSettings,
                messages: list[dict[str, Any]], on_content: Callable[[str], None] | None,
                attachment_ids: list[UUID] | None,
                on_progress: Callable[[MessageProgress], None] | None = None,
                on_round_start: Callable[[], None] | None = None) -> dict[str, Any]:
        """绑定各自权限与预算运行主从工具；主执行按需加载 Skill，子执行不继承共享工作目录写权限。"""
        from general_agent.memory.tools import agent_tools, worker_tools
        row = self.repository.info(run_id)
        worker = row['parent_agent_id'] is not None
        if worker and messages:
            messages = [{key: value for key, value in message.items() if key != '_current_request'} for message in messages]
            messages[-1] = {**messages[-1], '_current_request': True}
        checkpoint = RunCheckpoint(self.repository, run_id, user_id)
        counter = TokenCounter.for_settings(settings, self.tools.memory.directory)
        # 仅在当前会话实际选中知识库时向模型提供召回工具；执行时仍重新核验选择范围。
        knowledge_enabled = (self.tools.knowledge is not None and
                             bool(self.tools.knowledge.service.selected(row['conversation_id'], row['account_id'])))
        # 上下文准备已筛出当前会话的有效文字文档；纯图片和无附件时都不提供文档工具。
        documents_enabled = self.tools.attachments is not None and bool(attachment_ids)
        shared = worker_tools(knowledge_enabled, documents_enabled)
        tools = shared if worker else [*agent_tools(knowledge_enabled, documents_enabled), *orchestration_tools()]
        used_skills = (self.skills.service.conversation_skills(row['account_id'], row['conversation_id'])
                       if self.skills is not None and not worker else [])
        skill_session = SkillSession(self.skills, checkpoint, used_skills) if self.skills is not None and not worker else None
        if skill_session is not None:
            tools.extend(skill_tools()[:1])
            if skill_session.current_skills():
                tools.extend(skill_tools()[1:])
        injected: set[UUID] = set()
        document_budget = 0

        def before_model(current: list[dict[str, Any]], definitions: list[dict[str, Any]]) -> None:
            """外发前重验资料版本并拒绝超上下文，不静默截断消息。"""
            checkpoint.guard()
            if counter.available(settings, current, definitions) <= 0:
                raise ValueError('智能体上下文超过可用预算，请缩小任务范围。')

        def before_tool(index: int, call_id: str, name: str) -> None:
            """将工具幂等身份绑定模型轮次和原调用标识。"""
            checkpoint.guard()
            checkpoint.current_call = f'{index}:{call_id}'
            checkpoint.current_tool = name
            if on_progress is not None and not worker:
                on_progress(SUBAGENT_PROGRESS if name in ORCHESTRATION else tool_progress(name) or DRAFTING_PROGRESS)

        def stream_status(kind: str, attempt: int, total: int) -> None:
            """把当前智能体的断线恢复状态投影为主回答的临时进度。"""
            if on_progress is None or worker:
                return
            on_progress(connection_retry_progress(attempt, total) if kind == 'retrying'
                        else CONNECTION_RESUMED_PROGRESS)

        def budget(current: list[dict[str, Any]], definitions: list[dict[str, Any]]) -> None:
            """按本执行的实际消息核算读取预算，不借用其他子智能体用量。"""
            nonlocal document_budget
            document_budget = max(0, counter.available(settings, protocol_messages(current), definitions) - LIMITS.context_tool_reserve_tokens)
            with self.repository.database.connect() as connection:
                usage = connection.execute("SELECT prompt_tokens,completion_tokens FROM model_calls WHERE agent_run_id=%s AND unit='chat_tokens' ORDER BY created_at DESC,id DESC LIMIT 1", (run_id,)).fetchone()
            if usage is not None and usage['prompt_tokens'] is not None:
                last_assistant = max((index for index, message in enumerate(current) if message['role'] == 'assistant'), default=len(current))
                appended = counter.count(json.dumps(protocol_messages(current[last_assistant:]), ensure_ascii=False))
                observed = counter.input_limit(settings) - usage['prompt_tokens'] - (usage['completion_tokens'] or 0) - appended - LIMITS.context_tool_reserve_tokens
                document_budget = max(0, min(document_budget, observed))
            elif any(isinstance(item.get('content'), list) for item in current):
                document_budget = 0

        def child(child_run: UUID) -> None:
            """执行子任务及补充轮次，沿用子上下文且保存脱除推理的结果。"""
            child_info = self.repository.info(child_run)
            history = [item for item in child_info['messages'] if item.get('role') != 'system']
            history.append({'role': 'user', 'content': child_info['task']})
            if not child_info['messages']:
                material = self.tools.attachments.prepare(conversation_id=row['conversation_id'], account_id=row['account_id']) if self.tools.attachments else None
                history = [*cast(list[dict[str, Any]], material.messages if material else []), *history]
            try:
                output = self.execute(child_run, user_id, answer_id, settings, history, None, attachment_ids)
                self.repository.guard(child_run)
                clean = [{key: value for key, value in item.items() if key != 'reasoning_content'} for item in output['messages']]
                self.repository.transition(child_run, 'completed', output['answer'], clean)
            except ExecutionPaused:
                return
            except Exception as error:
                # 保留可公开的模型、预算或连接错误，避免前端只剩无法诊断的泛化提示。
                detail = str(error) if isinstance(error, (ValueError, LookupError, ModelFailure)) else '子任务未完成，已保留真实调用用量。'
                self.repository.transition(child_run, 'failed', error=detail)
                raise

        def execute_tool(current: MemoryCheckpoint, name: str, arguments: dict[str, Any], configured: StoredSettings) -> dict[str, Any]:
            """双重检查角色权限，Skill 工具仅在主执行中绑定当前 Run 执行。"""
            checkpoint.guard()
            try:
                if name in SKILL_TOOLS:
                    if skill_session is None:
                        raise ValueError('当前执行没有 Skill 工作目录权限。')
                    result = skill_session.execute(name, arguments)
                    if skill_session.current_skills():
                        graph.maximum_rounds = LIMITS.skill_rounds
                        if not any(tool['function']['name'] == 'read_skill_file' for tool in tools):
                            tools.extend(skill_tools()[1:])
                    return result
                if name in ORCHESTRATION:
                    if worker:
                        raise ValueError('子智能体没有编排权限。')
                    return self.orchestrator.execute(checkpoint, name, arguments, child)
                if worker and name not in WORKER_TOOLS:
                    raise ValueError('子智能体不能执行该工具。')
                result = self.tools.execute(
                    current,
                    name,
                    arguments,
                    configured,
                    document_budget=document_budget,
                    token_counter=counter,
                )
                checkpoint.guard()
                self.repository.record_sources(run_id, [result])
                return result
            finally:
                if on_progress is not None and not worker:
                    on_progress(DRAFTING_PROGRESS)

        def images(results: list[dict[str, Any]]) -> list[ModelMessage]:
            """加载真实资料原图及当前 Run 预览，工作图像只来自已授权文件。"""
            actual = [*results]
            for result in results:
                for agent in result.get('agents', []):
                    actual.append({'results': agent.get('sources', [])})
            knowledge = [result for result in actual if any('knowledge_base_id' in item for item in result.get('results', []))]
            attachments = [result for result in actual if result not in knowledge]
            output: list[ModelMessage] = []
            selected: list[UUID] = []
            try:
                if self.tools.attachments:
                    message, selected = self.tools.attachments.document_image_message(row['conversation_id'], row['account_id'], attachments, injected)
                    if message:
                        output.append(message)
                if self.tools.knowledge and len(selected) < 5:
                    message, added = self.tools.knowledge.image_message(row['conversation_id'], row['account_id'], knowledge, injected | set(selected), limit=5-len(selected))
                    selected.extend(added)
                    if message:
                        output.append(message)
            except (ValueError, LookupError, OSError, ModelFailure, httpx.HTTPError):
                output.append({'role': 'user', 'content': '部分原图无法加载，请明确尚未核对原图，仅根据文字描述回答。'})
            injected.update(selected)
            if skill_session is not None:
                output.extend(cast(list[ModelMessage], skill_session.images))
                skill_session.images.clear()
            checkpoint.guard()
            return output

        def receive(current: list[dict[str, Any]], key: str, wait: bool,
                    active_definitions: list[dict[str, Any]]) -> dict[str, Any]:
            """主动与被动交付共用信箱，以当前可用工具集合校验文本上下文预算。"""
            def validate(result: dict[str, Any]) -> None:
                """完整结果无法容纳时拒绝交付，不静默截断或丢弃未读事件。"""
                extra = {'role': 'user', 'content': json.dumps({'type': 'subagent_results', **result}, ensure_ascii=False)}
                if counter.available(settings, protocol_messages([*current, extra]), active_definitions) <= 0:
                    raise ValueError('子任务交付超过上下文预算，请缩小任务范围。')

            if wait and on_progress is not None:
                on_progress(SUBAGENT_PROGRESS)
            try:
                if wait:
                    return self.orchestrator.inbox.wait(checkpoint, key, validate)
                return self.orchestrator.inbox.collect(checkpoint, key, validate=validate)
            finally:
                if wait and on_progress is not None:
                    on_progress(DRAFTING_PROGRESS)

        def settle(current: list[dict[str, Any]], answer: str) -> bool:
            """主执行先收完子结果与制作命令，再提交最终回答，防止后台工作成为无主进程。"""
            if skill_session is not None and skill_session.current_skills():
                with self.repository.database.connect() as connection:
                    command = connection.execute(
                        "SELECT id FROM skill_commands WHERE run_id=%s AND status='running'",
                        (checkpoint.run_id,),
                    ).fetchone()
                if command:
                    result = skill_session.runtime.poll(checkpoint, command['id'], 0, LIMITS.skill_poll_seconds)
                    current.append({'role': 'user', 'content': '后台制作命令实际状态（请完成轮询后再交付）：' + json.dumps(result, ensure_ascii=False)})
                    return False
            return self.orchestrator.inbox.settle(checkpoint, answer, current)

        if not worker:
            with self.repository.database.connect() as connection:
                pending_children = connection.execute("SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.parent_agent_id=%s AND r.status='queued'", (row['agent_id'],)).fetchall()
            for pending in pending_children:
                child_id = pending['id']

                def resume_child(identifier: UUID = child_id) -> None:
                    """将恢复后的子执行重新提交队列，执行标识保持不变。"""
                    child(identifier)

                self.orchestrator.submit(child_id, resume_child)

        compactor = ContextCompactor(counter, settings, self.tools.memory.calls.bind(checkpoint.operation), answer_id)
        fragments = [SUBAGENT_PROMPT] if worker else []
        instructions = str(row.get('instructions') or '').strip()
        if worker and instructions:
            fragments.append(
                '本轮补充要求：\n'
                + instructions,
            )
        graph = AgentToolGraph(maximum_rounds=LIMITS.skill_rounds if skill_session and skill_session.current_skills()
                               else LIMITS.child_rounds if worker else LIMITS.main_rounds)
        try:
            if self.checkpoints:
                self.checkpoints.register('run:' + str(run_id), row['conversation_id'], answer_id)
            with (self.checkpoints.saver(row['conversation_id'], answer_id, 'run:' + str(run_id)) if self.checkpoints else nullcontext(None)) as saver:
                output = graph.run(messages=messages, settings=settings, calls=self.tools.memory.calls.bind(checkpoint.operation),
                    checkpoint=cast(MemoryCheckpoint, checkpoint), tools=tools, execute_tool=execute_tool,
                    context_fragments=fragments, assistant_message_id=answer_id, on_content=on_content,
                    instruction_context=skill_session.context if skill_session is not None else None,
                    on_tool_context=budget, on_tool_results=images, before_model=before_model, before_tool=before_tool,
                    on_stream_status=stream_status,
                    receive_inbox=None if worker else receive, settle_answer=None if worker else settle,
                    on_round_start=None if worker else on_round_start,
                    initial_used_skills=skill_session.current_skills() if skill_session else [],
                    current_used_skills=skill_session.current_skills if skill_session else None,
                    prepare_context=compactor.prepare, checkpointer=saver, thread_id='run:' + str(run_id))
        except Exception:
            checkpoint.guard()
            raise
        return dict(output)
