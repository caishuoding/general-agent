"""复用工具图执行主从智能体，隔离消息、证据和生命周期。"""

import json
import httpx
from typing import Any, TYPE_CHECKING, cast
from collections.abc import Callable
from uuid import UUID

from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.agent.graph import AgentToolGraph
from general_agent.agent.orchestration import AgentOrchestrator
from general_agent.agent.repository import AgentRepository, RunCheckpoint
from general_agent.agent.tools import ORCHESTRATION, WORKER_TOOLS, orchestration_tools
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelMessage, ModelFailure
from general_agent.infrastructure.settings import StoredSettings
from general_agent.infrastructure.tokens import TokenCounter
from general_agent.conversation.progress import DRAFTING_PROGRESS, SUBAGENT_PROGRESS, MessageProgress, tool_progress
from general_agent.memory.automatic import MemoryCheckpoint
from general_agent.prompts.agents import SUBAGENT_PROMPT

if TYPE_CHECKING:
    from general_agent.memory.tools import MemoryTools, ToolOutcome


class AgentRuntime:
    """将一次回答的主从运行集中在同一模块，调用者只需提供固定身份和材料。"""

    def __init__(self, tools: 'MemoryTools') -> None:
        """复用真实业务工具与调用网关，为应用绑定统一后台调度。"""
        self.tools = tools
        self.repository = AgentRepository(tools.memory.database)
        self.orchestrator = AgentOrchestrator(self.repository)

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
                raise ValueError('主回答尚未完成子任务收尾。')
            sources = self.repository.sources(root['id'])
            # 收尾只读取可信登记；这条本地证据快照不再发给模型。
            final_messages = result['messages']
            if sources:
                final_messages = [*final_messages[:-1], {'role': 'tool', 'content': json.dumps({'results': sources}, ensure_ascii=False)}, final_messages[-1]]
            return ToolOutcome(result['answer'], bool(result['used_tools']), final_messages)
        finally:
            self.repository.close(answer_id)

    def execute(self, run_id: UUID, user_id: UUID, answer_id: UUID, settings: StoredSettings,
                messages: list[dict[str, Any]], on_content: Callable[[str], None] | None,
                attachment_ids: list[UUID] | None,
                on_progress: Callable[[MessageProgress], None] | None = None,
                on_round_start: Callable[[], None] | None = None) -> dict[str, Any]:
        """按执行身份、模型配置和历史消息运行主或子任务；返回回答状态，并绑定各自权限与预算。"""
        from general_agent.memory.tools import agent_tools, worker_tools
        row = self.repository.info(run_id)
        worker = row['parent_agent_id'] is not None
        checkpoint = RunCheckpoint(self.repository, run_id, user_id)
        counter = TokenCounter.for_settings(settings, self.tools.memory.directory)
        shared = worker_tools(self.tools.knowledge is not None)
        tools = shared if worker else [*agent_tools(self.tools.knowledge is not None), *orchestration_tools()]
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

        def budget(current: list[dict[str, Any]], definitions: list[dict[str, Any]]) -> None:
            """按本执行的实际消息核算读取预算，不借用其他子智能体用量。"""
            nonlocal document_budget
            document_budget = max(0, counter.available(settings, current, definitions) - 256)
            with self.repository.database.connect() as connection:
                usage = connection.execute("SELECT prompt_tokens,completion_tokens FROM model_calls WHERE agent_run_id=%s AND unit='chat_tokens' ORDER BY created_at DESC,id DESC LIMIT 1", (run_id,)).fetchone()
            if usage is not None and usage['prompt_tokens'] is not None:
                last_assistant = max((index for index, message in enumerate(current) if message['role'] == 'assistant'), default=len(current))
                appended = counter.count(json.dumps(current[last_assistant:], ensure_ascii=False))
                observed = settings.context_window_tokens - settings.output_reserve_tokens - usage['prompt_tokens'] - (usage['completion_tokens'] or 0) - appended - 768
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
            except Exception:
                self.repository.transition(child_run, 'failed', error='子任务未完成，已保留真实调用用量。')
                raise

        def execute_tool(current: MemoryCheckpoint, name: str, arguments: dict[str, Any], configured: StoredSettings) -> dict[str, Any]:
            """双重执行角色权限检查，编排工具与业务工具使用各自可信入口。"""
            checkpoint.guard()
            try:
                if name in ORCHESTRATION:
                    if worker:
                        raise ValueError('子智能体没有编排权限。')
                    return self.orchestrator.execute(checkpoint, name, arguments, child)
                if worker and name not in WORKER_TOOLS:
                    raise ValueError('子智能体不能执行该工具。')
                result = self.tools.execute(current, name, arguments, configured, attachment_ids, document_budget, counter)
                checkpoint.guard()
                self.repository.record_sources(run_id, [result])
                return result
            finally:
                if on_progress is not None and not worker:
                    on_progress(DRAFTING_PROGRESS)

        def images(results: list[dict[str, Any]]) -> list[ModelMessage]:
            """加载本执行真实结果及已交付证据的原图，按批次去重并核验访问。"""
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
            checkpoint.guard()
            return output

        def receive(current: list[dict[str, Any]], key: str, wait: bool) -> dict[str, Any]:
            """主动与被动交付共用信箱，在标记已交付前校验文本上下文预算。"""
            def validate(result: dict[str, Any]) -> None:
                """完整结果无法容纳时拒绝交付，不静默截断或丢弃未读事件。"""
                extra = {'role': 'user', 'content': json.dumps({'type': 'subagent_results', **result}, ensure_ascii=False)}
                if counter.available(settings, [*current, extra], tools) <= 0:
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
            """主模型候选答案在无未读结果及活动子执行时原子完成，子模型不走此入口。"""
            return self.orchestrator.inbox.settle(checkpoint, answer, current)

        fragments = [SUBAGENT_PROMPT] if worker else []
        instructions = str(row.get('instructions') or '').strip()
        if worker and instructions:
            fragments.append(
                '本轮补充要求：\n'
                + instructions,
            )
        graph = AgentToolGraph(maximum_rounds=LIMITS.child_rounds if worker else LIMITS.main_rounds)
        output = graph.run(messages=messages, settings=settings, calls=self.tools.memory.calls.bind(checkpoint.operation),
            checkpoint=cast(MemoryCheckpoint, checkpoint), tools=tools, execute_tool=execute_tool,
            context_fragments=fragments, assistant_message_id=answer_id, on_content=on_content,
            on_tool_context=budget, on_tool_results=images, before_model=before_model, before_tool=before_tool,
            receive_inbox=None if worker else receive, settle_answer=None if worker else settle,
            on_round_start=None if worker else on_round_start)
        return dict(output)
