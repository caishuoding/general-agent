"""实现唯一 Agent 图的五个节点、工具循环和主从收尾。"""

import json
import logging
import time
from collections.abc import Callable
from typing import Any, Literal, cast
from uuid import UUID

import httpx
from langgraph.runtime import Runtime
from psycopg.types.json import Jsonb

from general_agent.agent.compaction import ContextCompactor
from general_agent.agent.context import AgentContext, AgentRole
from general_agent.agent.message_protocol import protocol_messages
from general_agent.agent.orchestration import AgentOrchestrator
from general_agent.agent.repository import AgentRepository, RunCheckpoint
from general_agent.agent.state import (
    AgentState,
    AgentStateUpdate,
    LoadedSkill,
    merge_agent_messages,
)
from general_agent.agent.tool_registry import build_tool_registry
from general_agent.agent.tools import ORCHESTRATION, WaitInput
from general_agent.chat.progress import (
    CONNECTION_RESUMED_PROGRESS,
    DRAFTING_PROGRESS,
    SUBAGENT_PROGRESS,
    connection_retry_progress,
    tool_progress,
)
from general_agent.chat.services import ChatServices
from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.infrastructure.provider import (
    ModelConnectionFailure,
    ModelFailure,
    ModelMessage,
)
from general_agent.infrastructure.tokens import TokenCounter
from general_agent.prompts import base_system_prompt
from general_agent.search.failures import WebRequestFailure
from general_agent.skills.context import build_skill_context
from general_agent.skills.execution import execute_skill_tool
from general_agent.skills.runtime import SkillRuntime
from general_agent.skills.tools import SKILL_TOOLS

LOGGER = logging.getLogger(__name__)
DOCUMENT_TOOLS = {'search_documents', 'read_document', 'search_knowledge'}
STREAM_RESUMED_MARKER = '[[STREAM_RESUMED]]'
STREAM_CONTINUATION_PROMPT = (
    '上一条 assistant 消息因连接中断在末尾停止。请从该消息的最后一个字符直接继续，'
    '保持原有格式、事实和引用；不要重复已经输出的内容，不要解释连接中断，'
    '不要重新执行已经完成的工具调用。'
)


def visible_stream_content(parts: list[str]) -> str:
    """移除仅供前端渲染的恢复标记，得到可重新发送给模型的已出现正文。"""
    return ''.join(parts).replace(STREAM_RESUMED_MARKER, '')


def tool_is_available(name: str, failures: dict[str, int], document_calls: dict[str, int],
                      maximum_tool_retries: int) -> bool:
    """按当前 Run 的失败和文档调用次数判断工具是否仍可调用。"""
    if name in DOCUMENT_TOOLS:
        return document_calls.get(name, 0) < LIMITS.document_calls
    return failures.get(name, 0) <= maximum_tool_retries


def available_tool_definitions(tools: list[dict[str, Any]], failures: dict[str, int],
                               document_calls: dict[str, int], maximum_tool_retries: int) -> list[dict[str, Any]]:
    """返回当前仍可调用的工具定义，供模型协议和额度控制共同使用。"""
    return [tool for tool in tools if tool_is_available(
        str(tool['function']['name']), failures, document_calls, maximum_tool_retries)]


def pending_tool_calls(state: AgentState) -> list[dict[str, Any]]:
    """从最后一条 assistant 消息读取当前待执行工具调用，避免重复存 State。"""
    if not state['messages']:
        return []
    last = state['messages'][-1]
    if last.get('role') != 'assistant':
        return []
    return [dict(call) for call in last.get('tool_calls') or [] if isinstance(call, dict)]


class AgentNodes:
    """绑定一次 Agent Run 的服务和额度，实现五个可恢复图节点。"""

    def __init__(
        self,
        *,
        services: ChatServices,
        checkpoint: RunCheckpoint,
        repository: AgentRepository,
        orchestrator: AgentOrchestrator,
        compactor: ContextCompactor,
        counter: TokenCounter,
        skill_runtime: SkillRuntime | None,
        run_child: Callable[[UUID], None],
    ) -> None:
        """保存本次 Run 的静态依赖；工具注册不会因 Skill 加载而改变。"""
        self.services = services
        self.checkpoint = checkpoint
        self.repository = repository
        self.orchestrator = orchestrator
        self.registry = build_tool_registry(AgentRole.MAIN if checkpoint.main else AgentRole.SUBAGENT)
        self.compactor = compactor
        self.counter = counter
        self.skill_runtime = skill_runtime
        self.run_child = run_child
        self.calls = services.memory.calls.bind(checkpoint.operation)
        self.last_visible_chunks: list[str] = []
        self.document_budget = 0
        self.injected_images: set[UUID] = set()
        self.maximum_tool_retries = LIMITS.tool_retries

    def prepare_context(self, state: AgentState, runtime: Runtime[AgentContext]) -> AgentStateUpdate:
        """准备当前 Run 的消息和可访问资源清单，主从使用同一节点。"""
        context = runtime.context
        try:
            if not context.is_main:
                attachments = self.services.attachment_access(context.conversation_id)
                knowledge_bases = self.services.knowledge_base_access(context.conversation_id)
                return {
                    'attachments': attachments,
                    'knowledge_bases': knowledge_bases,
                    'loaded_skills': [],
                }
            messages = self.services.prepare_response(
                context.conversation_id,
                context.account_id,
                context.user_message_id,
                context.operation,
                [message for message in state['messages']
                 if message.get('_context_summary')
                 or (message.get('role') in ('user', 'assistant') and not message.get('tool_calls'))],
            )
            loaded_skills = self._loaded_skill_access(context)
            attachments = self.services.attachment_access(context.conversation_id)
            knowledge_bases = self.services.knowledge_base_access(context.conversation_id)
            LOGGER.info(
                'event=context_access skills=%s attachments=%s knowledge_bases=%s',
                [item['name'] for item in loaded_skills], len(attachments), len(knowledge_bases))
            return {
                'messages': {'replace': [dict(message) for message in messages]},
                'attachments': attachments,
                'knowledge_bases': knowledge_bases,
                'loaded_skills': loaded_skills,
            }
        except (ValueError, LookupError, ModelFailure, WebRequestFailure) as error:
            LOGGER.warning('Agent 上下文准备失败', exc_info=True)
            return self._failure_update(state, context, error)
        except Exception:
            LOGGER.exception('Agent 上下文准备发生未分类异常')
            return self._failure_update(state, context, RuntimeError('上下文准备失败，请重试。'))

    def prepare_model(self, state: AgentState, runtime: Runtime[AgentContext]) -> AgentStateUpdate:
        """自动收信、压缩上下文并在每次模型请求前刷新动态指令。"""
        if state.get('error'):
            return {}
        context = runtime.context
        try:
            working = state
            if context.is_main and self._has_child_tasks(context):
                result = self._receive_inbox(
                    state['messages'], f"automatic:{state['model_round']}", False,
                    self._active_tools(state), context,
                )
                update = self._inbox_update(state, result, context)
                working = cast(AgentState, {**state, **update, 'messages': merge_agent_messages(
                    state['messages'], update.get('messages', []))})
            current = self._model_input(working, context, clean=False)
            updated = self.compactor.prepare(current, self._active_tools(working))
            conversation = [message for message in updated if message.get('role') != 'system']
            return {
                'messages': {'replace': [dict(message) for message in conversation]},
                'pending_agent_run_ids': self._pending_child_ids(context),
            }
        except (ValueError, LookupError, ModelFailure, WebRequestFailure) as error:
            LOGGER.warning('Agent 模型准备失败', exc_info=True)
            return self._failure_update(state, context, error)
        except Exception:
            LOGGER.exception('Agent 模型准备发生未分类异常')
            return self._failure_update(state, context, RuntimeError('模型准备失败，请重试。'))

    def call_model(self, state: AgentState, runtime: Runtime[AgentContext]) -> AgentStateUpdate:
        """调用模型并追加稳定 assistant 消息，失败时保留公开部分正文。"""
        context = runtime.context
        if state.get('error'):
            return {}
        try:
            index = int(state['model_round'])
            maximum = LIMITS.skill_rounds if context.is_main and state['loaded_skills'] else (
                LIMITS.main_rounds if context.is_main else LIMITS.child_rounds)
            if index >= maximum:
                raise ValueError('工具调用达到本次执行上限；已完成的操作保留，请缩小范围后继续。')
            key = self.checkpoint.key('tools:round', [context.settings.revision, index, state['messages']])
            reply = self.checkpoint.get(key)
            message_id = f'assistant:{context.run_id}:{index + 1}'
            if reply is None:
                reply = self._stream_model_reply(state, context, index, message_id)
                self.checkpoint.put(key, reply)
            else:
                reply = {**dict(reply), 'id': message_id}
            LOGGER.info('event=tool_model round=%s tools=%s', index, [
                item.get('function', {}).get('name') for item in reply.get('tool_calls') or []
            ])
            return {'messages': [reply], 'model_round': index + 1}
        except (ValueError, ModelFailure, WebRequestFailure) as error:
            LOGGER.warning('Agent 模型调用失败', exc_info=True)
            return self._failure_update(
                state, context, error, visible_stream_content(self.last_visible_chunks))
        except Exception:
            LOGGER.exception('Agent 模型调用发生未分类异常')
            return self._failure_update(
                state, context, RuntimeError('回答执行中断，请重试。'),
                visible_stream_content(self.last_visible_chunks),
            )

    def execute_tools(self, state: AgentState, runtime: Runtime[AgentContext]) -> AgentStateUpdate:
        """执行当前 assistant 消息中的工具批次，并更新额度与 Skill 清单。"""
        context = runtime.context
        try:
            return self._execute_tools_attempt(state, context)
        except (ValueError, LookupError, ModelFailure, WebRequestFailure) as error:
            return self._failure_update(state, context, error)
        except Exception:
            LOGGER.exception('Agent 工具执行发生未分类异常')
            return self._failure_update(state, context, RuntimeError('工具执行中断。'))

    def finalize(self, state: AgentState, runtime: Runtime[AgentContext]) -> AgentStateUpdate:
        """按 Agent 角色完成主回答或子任务收尾，并决定是否继续等待。"""
        context = runtime.context
        if not context.is_main:
            return {'finished': True}
        answer = self._current_output(state, context)
        current = [dict(message) for message in state['messages']]
        if not state.get('error') and not self._settle_main(current, answer):
            result = self._receive_inbox(
                current, f"settle:{state['model_round']}", True,
                self._active_tools(state), context,
            )
            update = self._inbox_update(state, result, context)
            return {
                **update,
                'messages': {'replace': merge_agent_messages(current, update.get('messages', []))},
                'finished': False,
            }
        return self._finish_main(state, context, answer, current)

    def _stream_model_reply(self, state: AgentState, context: AgentContext,
                            index: int, message_id: str) -> dict[str, Any]:
        """执行一次带断线续写的模型请求并形成完整 assistant 消息。"""
        if context.on_round_start is not None:
            context.on_round_start()
        content: list[str] = []
        self.last_visible_chunks.clear()
        reasoning: list[str] = []
        partial_calls: dict[int, dict[str, Any]] = {}
        attempt_messages = self._model_input(state, context)
        for attempt in range(1, LIMITS.stream_attempts + 1):
            if attempt > 1:
                prefix = visible_stream_content(content)
                if prefix.strip():
                    retry_messages: list[dict[str, Any]] = [
                        *self._model_input(state, context, clean=False),
                        {'role': 'assistant', 'content': prefix},
                        {'role': 'user', 'content': STREAM_CONTINUATION_PROMPT, '_context_pinned': True},
                    ]
                    retry_messages = self.compactor.prepare(retry_messages, self._active_tools(state))
                    attempt_messages = protocol_messages(retry_messages)
            self._before_model(attempt_messages, self._active_tools(state), context)
            attempt_calls: dict[int, dict[str, Any]] = {}
            attempt_reasoning: list[str] = []
            resume_marker_pending = bool(visible_stream_content(content).strip())
            try:
                for chunk in self.calls.stream(
                    context.settings,
                    cast(list[ModelMessage], attempt_messages),
                    assistant_message_id=context.assistant_message_id,
                    tools=self._active_tools(state) or None,
                ):
                    if chunk.content:
                        if resume_marker_pending:
                            content.append(STREAM_RESUMED_MARKER)
                            self.last_visible_chunks.append(STREAM_RESUMED_MARKER)
                            if context.on_content is not None:
                                context.on_content(STREAM_RESUMED_MARKER)
                            if context.on_progress is not None:
                                context.on_progress(CONNECTION_RESUMED_PROGRESS)
                            resume_marker_pending = False
                        content.append(chunk.content)
                        self.last_visible_chunks.append(chunk.content)
                        if context.on_content is not None:
                            context.on_content(chunk.content)
                    if chunk.reasoning_content:
                        attempt_reasoning.append(chunk.reasoning_content)
                    if chunk.tool_call is not None:
                        current = attempt_calls.setdefault(
                            chunk.tool_call.index,
                            {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}},
                        )
                        if chunk.tool_call.id:
                            current['id'] += chunk.tool_call.id
                        if chunk.tool_call.name:
                            current['function']['name'] += chunk.tool_call.name
                        if chunk.tool_call.arguments:
                            current['function']['arguments'] += chunk.tool_call.arguments
            except ModelConnectionFailure as error:
                if attempt >= LIMITS.stream_attempts:
                    raise ModelConnectionFailure(
                        f'模型连接中断，自动恢复 {LIMITS.stream_attempts - 1} 次后仍未成功。',
                        error.usage,
                    ) from None
                if context.on_progress is not None:
                    context.on_progress(connection_retry_progress(attempt, LIMITS.stream_attempts))
                self.checkpoint.guard()
                time.sleep(0.25 * attempt)
                self.checkpoint.guard()
                continue
            partial_calls = attempt_calls
            reasoning = attempt_reasoning
            break
        tool_calls = [partial_calls[item] for item in sorted(partial_calls)]
        for call in tool_calls:
            if not call['id'] or not call['function']['name']:
                raise ValueError('模型返回了不完整的工具调用。')
            if call['function']['name'] not in DOCUMENT_TOOLS and not isinstance(
                    json.loads(call['function']['arguments']), dict):
                raise ValueError('模型返回了无效的工具参数。')
        reply: dict[str, Any] = {
            'id': message_id,
            'role': 'assistant',
            'content': ''.join(content) or None,
            'tool_calls': tool_calls,
        }
        if reasoning:
            reply['reasoning_content'] = ''.join(reasoning)
        if not content and not tool_calls:
            raise ValueError('模型没有返回回答或工具调用。')
        return reply

    def _execute_tools_attempt(self, state: AgentState, context: AgentContext) -> AgentStateUpdate:
        """执行一批工具，保持文档额度、普通工具重试和 Skill 路径校验。"""
        tool_messages: list[dict[str, Any]] = []
        executed_results: list[dict[str, Any]] = []
        skill_images: list[dict[str, Any]] = []
        failures = dict(state['tool_failure_counts'])
        document_calls = dict(state['document_call_counts'])
        loaded_skills: list[LoadedSkill] = [cast(LoadedSkill, dict(skill)) for skill in state['loaded_skills']]
        self._active_tools(state)
        known_tools = self.registry.names
        for call in pending_tool_calls(state):
            function = call['function']
            name = str(function['name'])
            self._before_tool(context, int(state['model_round']) - 1, str(call['id']), name)
            try:
                arguments = json.loads(function['arguments'])
            except ValueError:
                if name not in DOCUMENT_TOOLS:
                    raise
                arguments = None
            result: dict[str, Any]
            if name in DOCUMENT_TOOLS:
                if name not in known_tools:
                    result = {'error': '未知工具，不允许执行。'}
                elif not tool_is_available(name, failures, document_calls, self.maximum_tool_retries):
                    result = {
                        'error': f'该工具在本次回答中最多调用 {LIMITS.document_calls} 次，已达到调用上限。',
                        'call_limit': LIMITS.document_calls,
                        'call_exhausted': True,
                    }
                else:
                    document_calls[name] = document_calls.get(name, 0) + 1
                    try:
                        if not isinstance(arguments, dict):
                            raise ValueError('工具参数必须是有效的 JSON 对象。')
                        if name == 'read_document':
                            self._update_document_budget({**state, 'messages': [*state['messages'], *tool_messages],
                                                          'document_call_counts': document_calls,
                                                          'tool_failure_counts': failures}, context)
                        result = self._execute_tool(name, arguments, context)
                    except (ValueError, LookupError, ModelFailure) as error:
                        result = {'error': str(error)}
                    result = {
                        **result,
                        'call_count': document_calls[name],
                        'call_limit': LIMITS.document_calls,
                        'call_exhausted': document_calls[name] >= LIMITS.document_calls,
                    }
                executed_results.append(result)
            elif name == 'wait_agents' and name in known_tools:
                WaitInput.model_validate(arguments)
                result = self._receive_inbox(
                    [*state['messages'], *tool_messages],
                    f"wait:{int(state['model_round']) - 1}:{call['id']}",
                    True,
                    self._active_tools(state),
                    context,
                )
                executed_results.append(result)
            else:
                def execute_registered(tool_name: str, tool_arguments: dict[str, Any],
                                       tool_context: AgentContext) -> dict[str, Any]:
                    """统一执行注册工具；Skill 加载与图片结果同步回本批 State。"""
                    if tool_name not in SKILL_TOOLS:
                        return self._execute_tool(tool_name, tool_arguments, tool_context)
                    if not context.is_main or self.skill_runtime is None:
                        raise ValueError('当前执行没有 Skill 工作目录权限。')
                    execution = execute_skill_tool(
                        {**state, 'loaded_skills': loaded_skills},
                        self.checkpoint, self.skill_runtime, tool_name, tool_arguments,
                    )
                    skill_images.extend(execution.image_messages)
                    if execution.loaded_skill is not None:
                        names = {item['name'] for item in loaded_skills}
                        if execution.loaded_skill['name'] not in names:
                            loaded_skills.append(execution.loaded_skill)
                    return execution.result

                result = self._execute_ordinary_tool(
                    state, context, call, arguments, failures, document_calls, execute_registered,
                )
                executed_results.append(result)
            tool_messages.append({
                'id': f"tool:{context.run_id}:{int(state['model_round']) - 1}:{call['id']}",
                'role': 'tool',
                'tool_call_id': str(call['id']),
                'content': json.dumps(result, ensure_ascii=False),
            })
        image_messages = self._tool_image_messages(executed_results, context)
        image_messages.extend(skill_images)
        tool_messages.extend(self._stable_image_messages(image_messages, state))
        pending = self._pending_child_ids(context) if context.is_main else []
        LOGGER.info('event=tool_execute names=%s', [
            call.get('function', {}).get('name') for call in pending_tool_calls(state)
        ])
        return {
            'messages': tool_messages,
            'loaded_skills': loaded_skills,
            'tool_failure_counts': failures,
            'document_call_counts': document_calls,
            'pending_agent_run_ids': pending,
        }

    def _execute_ordinary_tool(self, state: AgentState, context: AgentContext,
                               call: dict[str, Any], arguments: Any,
                               failures: dict[str, int], document_calls: dict[str, int],
                               executor: Callable[[str, dict[str, Any], AgentContext], dict[str, Any]],
                               ) -> dict[str, Any]:
        """为普通和 Skill 工具统一执行失败额度、执行派发与幂等缓存。"""
        function = call['function']
        name = str(function['name'])
        if not isinstance(arguments, dict):
            raise ValueError('工具参数必须是有效的 JSON 对象。')
        tool_key = self.checkpoint.key('tools:result', [context.settings.revision, int(state['model_round']) - 1, call])
        attempt_key = self.checkpoint.key('tools:attempt', [str(context.assistant_message_id), tool_key])
        web_tool = name in ('search_web', 'fetch_web')
        web_key = self.checkpoint.key('tools:web-failure', [str(context.assistant_message_id), name, arguments])
        provider_key = self.checkpoint.key('tools:web-provider-failure', [str(context.assistant_message_id)])
        cacheable = name not in {
            'get_current_time', 'poll_skill_command', 'load_skill',
            'read_skill_file', 'read_work_file', 'view_work_image', 'open_skill_preview',
        } and name not in ORCHESTRATION
        result = self.checkpoint.get(attempt_key) if cacheable else None
        if result is None and cacheable:
            cached = self.checkpoint.get(tool_key)
            if cached is not None and 'error' not in cached and cached.get('status') != 'failed':
                result = cached
        exhausted = not tool_is_available(name, failures, document_calls, self.maximum_tool_retries)
        blocked = (self.checkpoint.get(provider_key) or self.checkpoint.get(web_key)) if web_tool else None
        if blocked is not None:
            result = {**blocked, 'cached_failure': True}
        elif exhausted:
            result = {'error': '该工具已达到最大重试次数，本次回答中已禁用。'}
        elif result is None:
            if name not in self.registry.names:
                result = {'error': '未知工具，不允许执行。'}
            else:
                try:
                    result = executor(name, arguments, context)
                except WebRequestFailure as error:
                    result = error.tool_result()
                except (ValueError, LookupError, ModelFailure) as error:
                    result = {'error': str(error)}
        if web_tool and result.get('retry_managed_by') == 'backend':
            self.checkpoint.put(web_key, result)
            if result.get('tool_disabled'):
                if result.get('failure_scope') == 'execution':
                    failures[name] = self.maximum_tool_retries + 1
                else:
                    self.checkpoint.put(provider_key, result)
                    for tool_name in ('search_web', 'fetch_web'):
                        failures[tool_name] = self.maximum_tool_retries + 1
        elif web_tool and result.get('cached_failure'):
            pass
        elif 'error' in result or result.get('status') == 'failed':
            if not exhausted:
                failures[name] = failures.get(name, 0) + 1
            exhausted = failures.get(name, 0) > self.maximum_tool_retries
            result = {
                **result,
                'retry_count': max(0, failures[name] - 1),
                'retry_limit': self.maximum_tool_retries,
                'retry_exhausted': exhausted,
            }
            if exhausted:
                result['message'] = (
                    '该工具已达到最大重试次数，本次回答中已禁用。'
                    '请使用已有结果回答，或选择其他可用工具。'
                )
        if cacheable:
            failed = 'error' in result or result.get('status') == 'failed'
            self.checkpoint.put(attempt_key if failed else tool_key, result)
        return result

    def _execute_tool(self, name: str, arguments: dict[str, Any], context: AgentContext) -> dict[str, Any]:
        """按工具类别执行记忆、搜索、文档、知识库或后台编排工具。"""
        self.checkpoint.guard()
        try:
            if name in ORCHESTRATION:
                if not context.is_main:
                    raise ValueError('子智能体没有编排权限。')
                return self.orchestrator.execute(self.checkpoint, name, arguments, self.run_child)
            result = self.services.tools.execute(
                self.checkpoint, name, arguments, context.settings,
                document_budget=self.document_budget, token_counter=self.counter,
            )
            self.checkpoint.guard()
            self.repository.record_sources(context.run_id, [result])
            return result
        finally:
            if context.on_progress is not None and context.is_main:
                context.on_progress(DRAFTING_PROGRESS)

    def _model_input(self, state: AgentState, context: AgentContext,
                     clean: bool = True) -> list[dict[str, Any]]:
        """组装系统提示、Skill 路径和不可信资料，不持久化运行客户端。"""
        fragments = [fragment.strip() for fragment in context.context_fragments if fragment.strip()]
        if context.is_main and self.skill_runtime is not None:
            fragments.extend(build_skill_context(state, self.skill_runtime.service.catalog))
        prompt_parts = fragments if not context.is_main else [base_system_prompt(), *fragments]
        prompt = '\n\n'.join(dict.fromkeys(part for part in prompt_parts if part.strip()))
        messages = [{'role': 'system', 'content': prompt}, *state['messages']]
        return protocol_messages(messages) if clean else messages

    def _active_tools(self, state: AgentState) -> list[dict[str, Any]]:
        """返回当前仍可调用的静态注册工具。"""
        self.registry = build_tool_registry(
            AgentRole.MAIN if self.checkpoint.main else AgentRole.SUBAGENT,
            documents=any(item['kind'] == 'document' for item in state['attachments']),
            knowledge=bool(state['knowledge_bases']),
        )
        return available_tool_definitions(
            self.registry.definitions,
            state['tool_failure_counts'],
            state['document_call_counts'],
            self.maximum_tool_retries,
        )

    def _before_model(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                      context: AgentContext) -> None:
        """外发前重验资料版本并拒绝超出上下文预算的请求。"""
        self.checkpoint.guard()
        if self.counter.available(context.settings, messages, tools) <= 0:
            raise ValueError('智能体上下文超过可用预算，请缩小任务范围。')

    def _before_tool(self, context: AgentContext, index: int, call_id: str, name: str) -> None:
        """绑定工具幂等身份并投影当前公开进度。"""
        self.checkpoint.guard()
        self.checkpoint.current_call = f'{index}:{call_id}'
        self.checkpoint.current_tool = name
        if context.on_progress is not None and context.is_main:
            context.on_progress(SUBAGENT_PROGRESS if name in ORCHESTRATION else tool_progress(name) or DRAFTING_PROGRESS)

    def _update_document_budget(self, state: AgentState, context: AgentContext) -> None:
        """按当前模型输入和最近真实用量重新计算文档读取预算。"""
        messages = self._model_input(state, context)
        self.document_budget = max(
            0,
            self.counter.available(context.settings, protocol_messages(messages), self._active_tools(state)) - LIMITS.context_tool_reserve_tokens,
        )
        with self.repository.database.connect() as connection:
            usage = connection.execute(
                "SELECT prompt_tokens,completion_tokens FROM model_calls WHERE agent_run_id=%s "
                "AND unit='chat_tokens' ORDER BY created_at DESC,id DESC LIMIT 1",
                (context.run_id,),
            ).fetchone()
        if usage is not None and usage['prompt_tokens'] is not None:
            last_assistant = max((index for index, message in enumerate(messages)
                                  if message.get('role') == 'assistant'), default=len(messages))
            appended = self.counter.request_tokens(protocol_messages(messages[last_assistant:]), [])
            observed = (
                self.counter.input_limit(context.settings) - usage['prompt_tokens']
                - (usage['completion_tokens'] or 0) - appended - LIMITS.context_tool_reserve_tokens
            )
            self.document_budget = max(0, min(self.document_budget, observed))
        elif any(isinstance(item.get('content'), list) for item in messages):
            self.document_budget = 0

    def _tool_image_messages(self, results: list[dict[str, Any]], context: AgentContext) -> list[dict[str, Any]]:
        """加载真实资料原图，工作图像只来自当前 Run 的已授权文件。"""
        actual = [*results]
        for result in results:
            for agent in result.get('agents', []):
                actual.append({'results': agent.get('sources', [])})
        knowledge = [result for result in actual if any('knowledge_base_id' in item for item in result.get('results', []))]
        attachments = [result for result in actual if result not in knowledge]
        output: list[dict[str, Any]] = []
        selected: list[UUID] = []
        try:
            if self.services.attachments is not None:
                message, selected = self.services.attachments.document_image_message(
                    context.conversation_id, context.account_id, attachments, self.injected_images)
                if message:
                    output.append(cast(dict[str, Any], message))
            if self.services.knowledge is not None and len(selected) < 5:
                message, added = self.services.knowledge.image_message(
                    context.conversation_id, context.account_id, knowledge,
                    self.injected_images | set(selected), limit=5 - len(selected))
                selected.extend(added)
                if message:
                    output.append(cast(dict[str, Any], message))
        except (ValueError, LookupError, OSError, ModelFailure, httpx.HTTPError):
            output.append({'role': 'user', 'content': '部分原图无法加载，请明确尚未核对原图，仅根据文字描述回答。'})
        self.injected_images.update(selected)
        self.checkpoint.guard()
        return output

    def _stable_image_messages(self, messages: list[dict[str, Any]], state: AgentState) -> list[dict[str, Any]]:
        """为图片消息补充稳定 ID，避免检查点恢复时重复追加。"""
        result: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            item = dict(message)
            item['id'] = str(item.get('id') or f"image:{state['run_id']}:{state['model_round']}:{index}")
            result.append(item)
        return result

    def _receive_inbox(self, current: list[dict[str, Any]], key: str, wait: bool,
                       active_definitions: list[dict[str, Any]], context: AgentContext) -> dict[str, Any]:
        """主动与被动交付共用信箱，以当前工具集合校验上下文预算。"""
        def validate(result: dict[str, Any]) -> None:
            """完整结果无法容纳时拒绝交付，不静默截断未读事件。"""
            extra = {'role': 'user', 'content': json.dumps(
                {'type': 'subagent_results', **result}, ensure_ascii=False)}
            if self.counter.available(
                    context.settings, protocol_messages([*current, extra]), active_definitions) <= 0:
                raise ValueError('子任务交付超过上下文预算，请缩小任务范围。')

        if wait and context.on_progress is not None:
            context.on_progress(SUBAGENT_PROGRESS)
        try:
            if wait:
                return self.orchestrator.inbox.wait(self.checkpoint, key, validate)
            return self.orchestrator.inbox.collect(self.checkpoint, key, validate=validate)
        finally:
            if wait and context.on_progress is not None:
                context.on_progress(DRAFTING_PROGRESS)

    def _inbox_update(self, state: AgentState, result: dict[str, Any],
                      context: AgentContext) -> AgentStateUpdate:
        """把新批次作为不可信资料追加，并同步仍在运行的子任务 ID。"""
        delivery = str(result.get('delivery_id') or '')
        pending = self._pending_child_ids(context)
        if not result.get('agents') or not delivery:
            return {'pending_agent_run_ids': pending}
        extra: list[dict[str, Any]] = [{
            'id': f'subagent:{delivery}',
            'role': 'user',
            'content': json.dumps({'type': 'subagent_results', **result}, ensure_ascii=False),
        }]
        extra.extend(self._tool_image_messages([result], context))
        return {'messages': extra, 'pending_agent_run_ids': pending}

    def _has_child_tasks(self, context: AgentContext) -> bool:
        """存在子任务时才自动收信，同时覆盖已完成但尚未交付的结果。"""
        with self.repository.database.connect() as connection:
            return connection.execute(
                'SELECT 1 FROM agents WHERE answer_attempt_id=%s AND parent_agent_id IS NOT NULL LIMIT 1',
                (context.assistant_message_id,),
            ).fetchone() is not None

    def _pending_child_ids(self, context: AgentContext) -> list[str]:
        """返回主智能体当前仍在排队或运行的子任务 Run ID。"""
        if not context.is_main:
            return []
        with self.repository.database.connect() as connection:
            rows = connection.execute(
                "SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id "
                "WHERE a.answer_attempt_id=%s AND a.parent_agent_id IS NOT NULL "
                "AND r.status IN ('queued','running') ORDER BY r.sequence",
                (context.assistant_message_id,),
            ).fetchall()
        return [str(row['id']) for row in rows]

    def _settle_main(self, messages: list[dict[str, Any]], answer: str) -> bool:
        """确认后台命令和子任务均已结束后再提交主回答。"""
        if self.skill_runtime is not None:
            with self.repository.database.connect() as connection:
                command = connection.execute(
                    "SELECT id FROM skill_commands WHERE run_id=%s AND status='running'",
                    (self.checkpoint.run_id,),
                ).fetchone()
            if command:
                result = self.skill_runtime.poll(
                    self.checkpoint, command['id'], 0, LIMITS.skill_poll_seconds)
                messages.append({
                    'id': f"command-status:{command['id']}",
                    'role': 'user',
                    'content': '后台制作命令实际状态（请完成轮询后再交付）：' + json.dumps(result, ensure_ascii=False),
                })
                return False
        return self.orchestrator.inbox.settle(self.checkpoint, answer, messages)

    def _finish_main(self, state: AgentState, context: AgentContext,
                     answer: str, messages: list[dict[str, Any]]) -> AgentStateUpdate:
        """核验来源并提交最终答复；流式失败保留已展示正文，成功才整体替换。"""
        error = state.get('error')
        if error:
            self.services.search.fail(context.operation, error)
        answer, web_sources = self.services.search.resolve(context.operation, answer)
        registered_sources = self.repository.sources(context.run_id)
        candidates = [source for source in registered_sources
                      if isinstance(source.get('key'), str) and source['key'].startswith('K')]
        answer, knowledge_sources = self.services.resolve_knowledge_citations(candidates, answer)
        with self.repository.database.connect() as connection:
            completed = connection.execute(
                "UPDATE messages SET content=CASE WHEN %s AND content<>'' THEN content ELSE %s END,"
                "status=%s,error=%s,web_sources=%s,"
                "knowledge_sources=%s,progress=NULL WHERE id=%s AND status='generating' RETURNING id",
                (bool(error) and context.delivery == 'stream', answer,
                 'failed' if error else 'complete', error, Jsonb(web_sources),
                 Jsonb(knowledge_sources), context.assistant_message_id),
            ).fetchone()
        del completed
        self.repository.close(context.assistant_message_id)
        final_messages = self._answered_messages(messages, answer)
        final_messages[-1]['id'] = str(context.assistant_message_id)
        LOGGER.info('event=node finalize message_id=%s status=%s characters=%s',
                    context.assistant_message_id, 'failed' if error else 'complete', len(answer))
        return {
            'messages': {'replace': final_messages},
            'finished': True,
            'error': error,
            'pending_agent_run_ids': [],
        }

    def _failure_update(self, state: AgentState, context: AgentContext, error: Exception,
                        partial: str = '') -> AgentStateUpdate:
        """将致命错误转成统一 State，并为同步回答保留明确错误文本。"""
        detail = str(error)
        answer = partial or (detail if context.delivery == 'sync' else '')
        current = list(state['messages'])
        if answer:
            current.append({
                'id': f"assistant:{context.run_id}:{int(state['model_round']) + 1}",
                'role': 'assistant',
                'content': answer,
                'tool_calls': [],
            })
        return {'messages': {'replace': current}, 'error': detail, 'finished': False}

    def _current_output(self, state: AgentState, context: AgentContext) -> str:
        """从最后一条 assistant 消息提取公开输出，失败时按传输方式回退。"""
        for message in reversed(state['messages']):
            identifier = str(message.get('id') or '')
            if identifier != str(context.assistant_message_id) and not identifier.startswith(f'assistant:{context.run_id}:'):
                continue
            if message.get('role') == 'assistant' and not message.get('tool_calls'):
                return str(message.get('content') or '').strip()
        error = str(state.get('error') or '')
        return error if context.delivery == 'sync' else ''

    @staticmethod
    def _answered_messages(messages: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
        """用最终正文更新最后一条候选回答，保持消息完整且不重复。"""
        if messages and messages[-1].get('role') == 'assistant' and not messages[-1].get('tool_calls'):
            return [*messages[:-1], {**messages[-1], 'content': answer, 'tool_calls': []}]
        return [*messages, {'id': 'pending-final', 'role': 'assistant', 'content': answer, 'tool_calls': []}]

    def _loaded_skill_access(self, context: AgentContext) -> list[LoadedSkill]:
        """从会话 Skill 事实生成顶层 loaded_skills 路径清单。"""
        if self.skill_runtime is None or not context.is_main:
            return []
        service = self.skill_runtime.service
        names = service.conversation_skills(context.account_id, context.conversation_id)
        result: list[LoadedSkill] = []
        for name in names:
            try:
                item = service.catalog.describe(name)
            except ValueError:
                continue
            result.append({
                'name': name,
                'version': str(item['version']),
                'root': f'/opt/skills/{name}',
                'entry': f'/opt/skills/{name}/SKILL.md',
            })
        return result


def route_after_model(state: AgentState) -> Literal['execute_tools', 'finalize']:
    """失败直接收尾，存在工具调用进入工具节点，否则进入最终处理。"""
    if state.get('error'):
        return 'finalize'
    return 'execute_tools' if pending_tool_calls(state) else 'finalize'


def route_after_finalize(state: AgentState) -> Literal['prepare_model', '__end__']:
    """已结束或失败时终止，否则带着新的子任务结果回到模型准备。"""
    return '__end__' if state['finished'] or state.get('error') else 'prepare_model'
