"""使用 LangGraph 编排模型决策与业务工具执行。"""

import json
import logging
import time
from collections.abc import Callable
from typing import Any, Literal, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from general_agent.agent.message_protocol import protocol_messages
from general_agent.agent.state import AgentState
from general_agent.agent.tools import ORCHESTRATION
from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.infrastructure.calls import BoundCalls
from general_agent.infrastructure.checkpoints import STATE_VERSION
from general_agent.infrastructure.provider import ModelConnectionFailure, ModelFailure, ModelMessage
from general_agent.infrastructure.settings import StoredSettings
from general_agent.memory.automatic import MemoryCheckpoint
from general_agent.prompts import base_system_prompt
from general_agent.search.failures import WebRequestFailure


ToolExecutor = Callable[[MemoryCheckpoint, str, dict[str, Any], StoredSettings], dict[str, Any]]
ToolResultImages = Callable[[list[dict[str, Any]]], list[ModelMessage]]
InboxReceiver = Callable[[list[dict[str, Any]], str, bool, list[dict[str, Any]]], dict[str, Any]]
AnswerSettler = Callable[[list[dict[str, Any]], str], bool]
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
    """按当前回答的失败和文档调用次数判断工具是否仍可调用。"""
    if name in DOCUMENT_TOOLS:
        return document_calls.get(name, 0) < LIMITS.document_calls
    return failures.get(name, 0) <= maximum_tool_retries


def available_tool_definitions(tools: list[dict[str, Any]], failures: dict[str, int],
                               document_calls: dict[str, int], maximum_tool_retries: int) -> list[dict[str, Any]]:
    """返回当前仍可调用的工具定义，供模型协议和各类预算共同使用。"""
    return [tool for tool in tools if tool_is_available(
        tool['function']['name'], failures, document_calls, maximum_tool_retries)]




class AgentToolGraph:
    """构建并运行 LangGraph 工具循环，同时复用现有业务工具和幂等检查点。"""

    def __init__(self, maximum_rounds: int = 12, maximum_tool_retries: int = LIMITS.tool_retries) -> None:
        """设置回答轮数和项目统一的工具重试上限；重试次数不包含首次失败调用。"""
        if maximum_rounds < 1 or maximum_tool_retries < 0:
            raise ValueError("模型轮数必须为正数，工具重试次数不能为负数。")
        self.maximum_rounds = maximum_rounds
        self.maximum_tool_retries = maximum_tool_retries

    def run(
        self,
        *,
        messages: list[dict[str, Any]],
        settings: StoredSettings,
        calls: BoundCalls,
        checkpoint: MemoryCheckpoint,
        tools: list[dict[str, Any]],
        execute_tool: ToolExecutor,
        context_fragments: list[str] | None = None,
        instruction_context: Callable[[], list[str]] | None = None,
        assistant_message_id: UUID | None = None,
        on_content: Callable[[str], None] | None = None,
        on_tool_context: Callable[[list[dict[str, Any]], list[dict[str, Any]]], None] | None = None,
        on_tool_results: ToolResultImages | None = None,
        before_model: Callable[[list[dict[str, Any]], list[dict[str, Any]]], None] | None = None,
        before_tool: Callable[[int, str, str], None] | None = None,
        on_stream_status: Callable[[str, int, int], None] | None = None,
        receive_inbox: InboxReceiver | None = None,
        settle_answer: AnswerSettler | None = None,
        on_round_start: Callable[[], None] | None = None,
        runtime_state: Callable[[], dict[str, Any]] | None = None,
        prepare_context: Callable[[list[dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        thread_id: str | None = None,
    ) -> AgentState:
        """恢复原执行的模型工具循环，完整步骤通过检查点保存且每轮重建可信指令。"""

        def inbox_update(state: AgentState, result: dict[str, Any]) -> dict[str, Any]:
            """将新批次作为不可信资料追加到 State，同时复用原图加载和批次去重。"""
            delivery = str(result.get('delivery_id') or '')
            watching = bool(result.get('active_count'))
            if not result.get('agents') or delivery in state['inbox_deliveries']:
                return {'watching_inbox': watching}
            extra: list[dict[str, Any]] = [{'role': 'user', 'content': json.dumps(
                {'type': 'subagent_results', **result}, ensure_ascii=False)}]
            if on_tool_results is not None:
                extra.extend(cast(list[dict[str, Any]], on_tool_results([result])))
            return {'messages': [*state['messages'], *extra],
                    'inbox_deliveries': [*state['inbox_deliveries'], delivery],
                    'watching_inbox': watching}

        def collect_inbox(state: AgentState) -> dict[str, Any]:
            """完整工具回复之后自动领取当前回答的结果，等待逻辑不占模型轮次。"""
            if receive_inbox is None:
                return {}
            result = receive_inbox(state['messages'], f'automatic:{state["round_index"]}', False, active_tools(state))
            return inbox_update(state, result)

        def model_input(current: list[dict[str, Any]], clean: bool = True) -> list[dict[str, Any]]:
            """统一组装系统提示与动态指令，可保留内部标记交给压缩器。"""
            system_parts = [str(message['content']) for message in current if message.get('role') == 'system']
            fragments = [fragment.strip() for fragment in (context_fragments or []) if fragment.strip()]
            if instruction_context is not None:
                fragments.extend(instruction_context())
            prompt = '\n\n'.join([*system_parts, *fragments]) if system_parts or fragments else base_system_prompt()
            messages = [{'role': 'system', 'content': prompt}, *(message for message in current if message.get('role') != 'system')]
            return protocol_messages(messages) if clean else messages

        def active_tools(state: AgentState) -> list[dict[str, Any]]:
            """返回当前回答仍可调用的工具，供模型、压缩和执行预算共同使用。"""
            return available_tool_definitions(
                tools, state['tool_failures'], state['document_calls'], self.maximum_tool_retries)

        def prepare_model(state: AgentState) -> dict[str, Any]:
            """模型请求前压缩旧交互，节点返回后先持久化再执行实际模型调用。"""
            if prepare_context is None:
                return {}
            updated = prepare_context(model_input(state['messages'], clean=False), active_tools(state))
            return {'runtime_state': runtime_state() if runtime_state else {},
                'messages': [*(message for message in state['messages'] if message.get('role') == 'system'),
                *(message for message in updated if message.get('role') != 'system')]}

        def call_model(state: AgentState) -> dict[str, Any]:
            """组装完整活动指令，每次实际外发及断线续写都按请求重新检查预算。"""
            index = state["round_index"]
            if index >= self.maximum_rounds:
                raise ValueError("工具调用达到本次执行上限；已完成的操作保留，请缩小范围后继续。")
            key = checkpoint.key("tools:round", [settings.revision, index, state["messages"]])
            reply = checkpoint.get(key)
            if reply is None:
                model_messages = model_input(state['messages'])
                available_tools = active_tools(state)
                if on_round_start is not None:
                    on_round_start()
                content: list[str] = []
                reasoning: list[str] = []
                partial_calls: dict[int, dict[str, Any]] = {}
                attempt_messages = model_messages
                for attempt in range(1, LIMITS.stream_attempts + 1):
                    if attempt > 1:
                        prefix = visible_stream_content(content)
                        if prefix.strip():
                            retry_messages: list[dict[str, Any]] = [
                                *model_input(state['messages'], clean=False),
                                {'role': 'assistant', 'content': prefix},
                                {'role': 'user', 'content': STREAM_CONTINUATION_PROMPT, '_context_pinned': True},
                            ]
                            if prepare_context is not None:
                                retry_messages = prepare_context(retry_messages, available_tools)
                            attempt_messages = protocol_messages(retry_messages)
                    if before_model is not None:
                        before_model(attempt_messages, available_tools)
                    attempt_calls: dict[int, dict[str, Any]] = {}
                    attempt_reasoning: list[str] = []
                    resume_marker_pending = bool(visible_stream_content(content).strip())
                    try:
                        for chunk in calls.stream(
                            settings,
                            cast(list[ModelMessage], attempt_messages),
                            assistant_message_id=assistant_message_id,
                            tools=available_tools or None,
                        ):
                            if chunk.content:
                                # 第一批恢复内容前写入持久标记，用户能看到真实的断点位置。
                                if resume_marker_pending:
                                    content.append(STREAM_RESUMED_MARKER)
                                    if on_content is not None:
                                        on_content(STREAM_RESUMED_MARKER)
                                    if on_stream_status is not None:
                                        on_stream_status('resumed', attempt - 1, LIMITS.stream_attempts)
                                    resume_marker_pending = False
                                content.append(chunk.content)
                                if on_content is not None:
                                    on_content(chunk.content)
                            if chunk.reasoning_content:
                                attempt_reasoning.append(chunk.reasoning_content)
                            if chunk.tool_call is not None:
                                current = attempt_calls.setdefault(
                                    chunk.tool_call.index,
                                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                                )
                                if chunk.tool_call.id:
                                    current["id"] += chunk.tool_call.id
                                if chunk.tool_call.name:
                                    current["function"]["name"] += chunk.tool_call.name
                                if chunk.tool_call.arguments:
                                    current["function"]["arguments"] += chunk.tool_call.arguments
                    except ModelConnectionFailure as error:
                        if attempt >= LIMITS.stream_attempts:
                            raise ModelConnectionFailure(
                                f'模型连接中断，自动恢复 {LIMITS.stream_attempts - 1} 次后仍未成功。',
                                error.usage,
                            ) from None
                        if on_stream_status is not None:
                            on_stream_status('retrying', attempt, LIMITS.stream_attempts)
                        checkpoint.guard()
                        time.sleep(0.25 * attempt)
                        checkpoint.guard()
                        continue
                    partial_calls = attempt_calls
                    reasoning = attempt_reasoning
                    break
                tool_calls = [partial_calls[item] for item in sorted(partial_calls)]
                for call in tool_calls:
                    if not call["id"] or not call["function"]["name"]:
                        raise ValueError("模型返回了不完整的工具调用。")
                    if call['function']['name'] not in DOCUMENT_TOOLS and not isinstance(json.loads(call["function"]["arguments"]), dict):
                        raise ValueError("模型返回了无效的工具参数。")
                reply = {
                    "role": "assistant",
                    "content": "".join(content) or None,
                    "tool_calls": tool_calls,
                }
                if reasoning:
                    reply["reasoning_content"] = "".join(reasoning)
                if not content and not tool_calls:
                    raise ValueError("模型没有返回回答或工具调用。")
                checkpoint.put(key, reply)
            tool_calls = reply.get("tool_calls") or []
            LOGGER.info("event=tool_model round=%s tools=%s", index, [item.get("function", {}).get("name") for item in tool_calls])
            return {
                "messages": [*state["messages"], reply],
                "tool_calls": tool_calls,
                "round_index": index + 1,
            }

        def route_model(state: AgentState) -> Literal["execute_tools", "settle_answer"]:
            """模型无工具调用时先检查子任务收尾，避免提前结束并丢弃完成结果。"""
            return "execute_tools" if state["tool_calls"] else "settle_answer"

        def execute_tools(state: AgentState) -> dict[str, Any]:
            """按输入状态执行工具批次，返回工具消息与额度状态；联网最终失败缓存，分类耗尽只禁用同类工具。"""
            tool_messages: list[dict[str, Any]] = []
            executed_results: list[dict[str, Any]] = []
            failures = dict(state["tool_failures"])
            document_calls = dict(state['document_calls'])
            known_tools = {tool["function"]["name"] for tool in tools}
            for call in state["tool_calls"]:
                if before_tool is not None:
                    before_tool(state["round_index"] - 1, call["id"], call["function"]["name"])
                result: dict[str, Any] | None = None
                function = call["function"]
                name = function["name"]
                try:
                    arguments = json.loads(function['arguments'])
                except ValueError:
                    if name not in DOCUMENT_TOOLS:
                        raise
                    arguments = None
                if name in DOCUMENT_TOOLS:
                    if name not in known_tools:
                        result = {'error': '未知工具，不允许执行。'}
                    elif not tool_is_available(name, failures, document_calls, self.maximum_tool_retries):
                        result = {'error': f'该工具在本次回答中最多调用 {LIMITS.document_calls} 次，已达到调用上限。',
                                  'call_limit': LIMITS.document_calls, 'call_exhausted': True}
                    else:
                        document_calls[name] = document_calls.get(name, 0) + 1
                        try:
                            if not isinstance(arguments, dict):
                                raise ValueError('工具参数必须是有效的 JSON 对象。')
                            if name == 'read_document' and on_tool_context is not None:
                                on_tool_context(
                                    [*state['messages'], *tool_messages],
                                    available_tool_definitions(tools, failures, document_calls, self.maximum_tool_retries),
                                )
                            result = execute_tool(checkpoint, name, arguments, settings)
                        except (ValueError, LookupError, ModelFailure) as error:
                            result = {'error': str(error)}
                        # 文档读取不复用历史工具缓存，避免到期或删除后重放旧正文。
                        result = {**result, 'call_count': document_calls[name], 'call_limit': LIMITS.document_calls,
                                  'call_exhausted': document_calls[name] >= LIMITS.document_calls}
                    tool_messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                          'content': json.dumps(result, ensure_ascii=False)})
                    executed_results.append(result)
                    continue
                if name == 'wait_agents' and name in known_tools:
                    # 等待基础设施故障应终止执行，不套用普通工具的失败禁用策略。
                    if receive_inbox is None:
                        result = execute_tool(checkpoint, name, arguments, settings)
                    else:
                        from general_agent.agent.tools import WaitInput
                        WaitInput.model_validate(arguments)
                        result = receive_inbox([*state['messages'], *tool_messages],
                                               f'wait:{state["round_index"] - 1}:{call["id"]}', True,
                                               available_tool_definitions(tools, failures, document_calls, self.maximum_tool_retries))
                    tool_messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                          'content': json.dumps(result, ensure_ascii=False)})
                    executed_results.append(result)
                    continue
                tool_key = checkpoint.key("tools:result", [settings.revision, state["round_index"] - 1, call])
                attempt_key = checkpoint.key("tools:attempt", [str(assistant_message_id), tool_key])
                web_tool = name in ('search_web', 'fetch_web')
                web_key = checkpoint.key('tools:web-failure', [str(assistant_message_id), name, arguments])
                provider_key = checkpoint.key('tools:web-provider-failure', [str(assistant_message_id)])
                # 当前时间每次执行都重新读取，不复用历史步骤中的旧时钟。
                cacheable = name not in {'get_current_time', 'poll_skill_command', 'load_skill', 'read_skill_file', 'open_skill_preview'} and name not in ORCHESTRATION
                result = checkpoint.get(attempt_key) if cacheable else None
                if result is None and cacheable:
                    cached = checkpoint.get(tool_key)
                    if cached is not None and "error" not in cached and cached.get("status") != "failed":
                        result = cached
                exhausted = not tool_is_available(name, failures, document_calls, self.maximum_tool_retries)
                blocked = (checkpoint.get(provider_key) or checkpoint.get(web_key)) if web_tool else None
                if blocked is not None:
                    result = {**blocked, 'cached_failure': True}
                elif exhausted:
                    # 即使模型忽略可用工具列表，后端也不得继续执行已禁用工具。
                    result = {"error": "该工具已达到最大重试次数，本次回答中已禁用。"}
                elif result is None:
                    if name not in known_tools:
                        result = {"error": "未知工具，不允许执行。"}
                    else:
                        try:
                            result = execute_tool(checkpoint, name, arguments, settings)
                        except WebRequestFailure as error:
                            result = error.tool_result()
                        except (ValueError, LookupError, ModelFailure) as error:
                            result = {"error": str(error)}
                if web_tool and result.get('retry_managed_by') == 'backend':
                    # 同一参数的最终失败跨模型轮次复用，新的 URL／查询不受影响。
                    checkpoint.put(web_key, result)
                    if result.get('tool_disabled'):
                        if result.get('failure_scope') == 'execution':
                            # 分类额度仅禁用当前工具，另一类联网请求继续使用自己的额度。
                            failures[name] = self.maximum_tool_retries + 1
                        else:
                            checkpoint.put(provider_key, result)
                            for tool_name in ('search_web', 'fetch_web'):
                                failures[tool_name] = self.maximum_tool_retries + 1
                elif name in known_tools and ("error" in result or result.get("status") == "failed"):
                    if not exhausted:
                        failures[name] = failures.get(name, 0) + 1
                    exhausted = failures.get(name, 0) > self.maximum_tool_retries
                    result = {
                        **result,
                        "retry_count": max(0, failures[name] - 1),
                        "retry_limit": self.maximum_tool_retries,
                        "retry_exhausted": exhausted,
                    }
                    if exhausted:
                        result["message"] = (
                            "该工具已达到最大重试次数，本次回答中已禁用。"
                            "请使用已有结果回答，或选择其他可用工具。"
                        )
                # 失败只属于当前回答，成功步骤跨回答重试复用，避免丢失已观察的记忆目标。
                failed = "error" in result or result.get("status") == "failed"
                if cacheable:
                    checkpoint.put(attempt_key if failed else tool_key, result)
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })
                executed_results.append(result)
            if on_tool_results is not None and executed_results:
                tool_messages.extend(cast(list[dict[str, Any]], on_tool_results(executed_results)))
            names = [call.get("function", {}).get("name") for call in state["tool_calls"]]
            watching = state['watching_inbox'] or any(name in ORCHESTRATION for name in names)
            for result in executed_results:
                if 'active_count' in result:
                    watching = bool(result['active_count'])
            LOGGER.info("event=tool_execute names=%s", names)
            return {
                "messages": [*state["messages"], *tool_messages],
                "tool_calls": [],
                "used_tools": True,
                "tool_failures": failures,
                "document_calls": document_calls,
                'watching_inbox': watching,
            }

        def route_tools(state: AgentState) -> Literal['collect_inbox', 'call_model']:
            """仅在编排工具启用过信箱时于工具批次后收信，普通业务工具直接回模型。"""
            return 'collect_inbox' if state['watching_inbox'] else 'call_model'

        def settle_candidate(state: AgentState) -> dict[str, Any]:
            """子任务未结束则后端等待，收到结果后继续分析，否则提交候选答案。"""
            answer = str(state['messages'][-1].get('content') or '').strip()
            if settle_answer is None or settle_answer(state['messages'], answer):
                return {'settled': True}
            assert receive_inbox is not None
            result = receive_inbox(state['messages'], f'settle:{state["round_index"]}', True, active_tools(state))
            return {**inbox_update(state, result), 'settled': False}

        def route_settle(state: AgentState) -> Literal['finish', 'collect_inbox']:
            """只允许已确认关闭的候选答案结束，其他情况进入下一轮收信。"""
            return 'finish' if state['settled'] else 'collect_inbox'

        def finish(state: AgentState) -> dict[str, Any]:
            """返回最后一轮流式文本作为回答，并保留是否执行过工具。"""
            reply = state["messages"][-1]
            answer = str(reply.get("content") or "").strip()
            LOGGER.info("event=tool_finish used_tools=%s round=%s", state["used_tools"], state["round_index"])
            return {"answer": answer or None}

        builder = StateGraph(AgentState)
        builder.add_node('prepare_model', prepare_model)
        builder.add_node('collect_inbox', collect_inbox)
        builder.add_node("call_model", call_model)
        builder.add_node("execute_tools", execute_tools)
        builder.add_node("finish", finish)
        builder.add_node('settle_answer', settle_candidate)
        builder.add_edge(START, 'prepare_model')
        builder.add_edge('prepare_model', 'call_model')
        builder.add_edge('collect_inbox', 'prepare_model')
        builder.add_conditional_edges(
            "call_model",
            route_model,
            {"execute_tools": "execute_tools", "settle_answer": "settle_answer"},
        )
        builder.add_conditional_edges('execute_tools', route_tools,
                                      {'collect_inbox': 'collect_inbox', 'call_model': 'prepare_model'})
        builder.add_conditional_edges('settle_answer', route_settle,
                                      {'finish': 'finish', 'collect_inbox': 'collect_inbox'})
        builder.add_edge("finish", END)
        graph = builder.compile(checkpointer=checkpointer)
        raw_messages = [*messages]
        initial: AgentState = {
            "version": STATE_VERSION, "runtime_state": runtime_state() if runtime_state else {},
            "messages": raw_messages,
            "tool_calls": [],
            "round_index": 0,
            "used_tools": False,
            "tool_failures": {},
            "document_calls": {},
            "answer": None,
            'inbox_deliveries': [],
            'settled': False,
            'watching_inbox': False,
        }
        # Skill 可在运行中把业务轮数上限提高到 128；图递归上限必须提前预留，
        # 否则 LangGraph 会在调用模型的业务上限生效前截断执行。
        recursion_rounds = (max(self.maximum_rounds, LIMITS.skill_rounds)
                            if instruction_context is not None else self.maximum_rounds)
        config: Any = {
            'configurable': {'thread_id': thread_id or str(checkpoint.message_id)},
            'recursion_limit': recursion_rounds * 4 + 8,
        }
        saved = graph.get_state(config) if checkpointer is not None else None
        if saved and saved.values:
            if saved.values.get("version") != STATE_VERSION:
                raise ValueError("Agent 检查点版本不兼容，请重试。")
            if saved.next:
                return cast(AgentState, graph.invoke(None, config, durability='sync'))
            return cast(AgentState, saved.values)
        return cast(AgentState, graph.invoke(initial, config, durability='sync' if checkpointer is not None else None))
