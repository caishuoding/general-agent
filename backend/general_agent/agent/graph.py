"""使用 LangGraph 编排模型决策与业务工具执行。"""

import json
import logging
from collections.abc import Callable
from typing import Any, Literal, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from general_agent.agent.state import AgentState
from general_agent.agent.tools import ORCHESTRATION
from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.infrastructure.calls import BoundCalls
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.infrastructure.settings import StoredSettings
from general_agent.memory.automatic import MemoryCheckpoint
from general_agent.prompts import compose_system_prompt
from general_agent.search.failures import WebRequestFailure


ToolExecutor = Callable[[MemoryCheckpoint, str, dict[str, Any], StoredSettings], dict[str, Any]]
ToolResultImages = Callable[[list[dict[str, Any]]], list[ModelMessage]]
InboxReceiver = Callable[[list[dict[str, Any]], str, bool], dict[str, Any]]
AnswerSettler = Callable[[list[dict[str, Any]], str], bool]
LOGGER = logging.getLogger(__name__)
DOCUMENT_TOOLS = {'search_documents', 'read_document', 'search_knowledge'}


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
        context_messages: list[dict[str, Any]] | None = None,
        context_fragments: list[str] | None = None,
        assistant_message_id: UUID | None = None,
        on_content: Callable[[str], None] | None = None,
        on_tool_context: Callable[[list[dict[str, Any]], list[dict[str, Any]]], None] | None = None,
        on_tool_results: ToolResultImages | None = None,
        before_model: Callable[[list[dict[str, Any]], list[dict[str, Any]]], None] | None = None,
        before_tool: Callable[[int, str, str], None] | None = None,
        receive_inbox: InboxReceiver | None = None,
        settle_answer: AnswerSettler | None = None,
        on_round_start: Callable[[], None] | None = None,
    ) -> AgentState:
        """按消息、配置和工具运行模型循环；返回含回答与消息快照的状态，并在收尾时接收子任务结果。"""

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
            result = receive_inbox(state['messages'], f'automatic:{state["round_index"]}', False)
            return inbox_update(state, result)

        def call_model(state: AgentState) -> dict[str, Any]:
            """根据当前状态组装单条系统消息和可用工具；返回模型回复及更新后的轮次状态。"""
            if before_model is not None:
                before_model(state["messages"], tools)
            index = state["round_index"]
            if index >= self.maximum_rounds:
                raise ValueError("工具调用达到本次执行上限；已完成的操作保留，请缩小范围后继续。")
            if on_round_start is not None:
                on_round_start()
            key = checkpoint.key("tools:round", [settings.revision, index, state["messages"]])
            reply = checkpoint.get(key)
            if reply is None:
                # 准备阶段可能已附加资料说明；将其与角色规则合成唯一的系统消息。
                system_parts = [str(message['content']) for message in state['messages']
                                if message.get('role') == 'system']
                fragments = [*(context_fragments or [])]
                system_prompt = ('\n\n'.join([*system_parts, *fragments]) if system_parts
                                 else compose_system_prompt(*fragments))
                model_messages = [
                    {"role": "system", "content": system_prompt},
                    *(message for message in state['messages'] if message.get('role') != 'system'),
                ]
                content: list[str] = []
                reasoning: list[str] = []
                partial_calls: dict[int, dict[str, Any]] = {}
                for chunk in calls.stream(
                    settings,
                    cast(list[ModelMessage], model_messages),
                    assistant_message_id=assistant_message_id,
                    tools=[tool for tool in tools
                           if (state['document_calls'].get(tool['function']['name'], 0) < 5
                               if tool['function']['name'] in DOCUMENT_TOOLS else
                               state['tool_failures'].get(tool['function']['name'], 0) <= self.maximum_tool_retries)] or None,
                ):
                    if chunk.content:
                        content.append(chunk.content)
                        if on_content is not None:
                            on_content(chunk.content)
                    if chunk.reasoning_content:
                        reasoning.append(chunk.reasoning_content)
                    if chunk.tool_call is not None:
                        current = partial_calls.setdefault(
                            chunk.tool_call.index,
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        if chunk.tool_call.id:
                            current["id"] += chunk.tool_call.id
                        if chunk.tool_call.name:
                            current["function"]["name"] += chunk.tool_call.name
                        if chunk.tool_call.arguments:
                            current["function"]["arguments"] += chunk.tool_call.arguments
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
            """检查限额并缓存最终联网失败；后端已处理重试的结果不再套用模型重试循环。"""
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
                    elif document_calls.get(name, 0) >= 5:
                        result = {'error': '该工具本轮最多调用五次，已达到调用上限。', 'call_limit': 5, 'call_exhausted': True}
                    else:
                        document_calls[name] = document_calls.get(name, 0) + 1
                        try:
                            if not isinstance(arguments, dict):
                                raise ValueError('工具参数必须是有效的 JSON 对象。')
                            if name == 'read_document' and on_tool_context is not None:
                                on_tool_context([*state['messages'], *tool_messages], tools)
                            result = execute_tool(checkpoint, name, arguments, settings)
                        except (ValueError, LookupError, ModelFailure) as error:
                            result = {'error': str(error)}
                        # 文档读取不复用历史工具缓存，避免到期或删除后重放旧正文。
                        result = {**result, 'call_count': document_calls[name], 'call_limit': 5,
                                  'call_exhausted': document_calls[name] >= 5}
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
                                               f'wait:{state["round_index"] - 1}:{call["id"]}', True)
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
                cacheable = name != 'get_current_time' and name not in ORCHESTRATION
                result = checkpoint.get(attempt_key) if cacheable else None
                if result is None and cacheable:
                    cached = checkpoint.get(tool_key)
                    if cached is not None and "error" not in cached and cached.get("status") != "failed":
                        result = cached
                exhausted = failures.get(name, 0) > self.maximum_tool_retries
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
            result = receive_inbox(state['messages'], f'settle:{state["round_index"]}', True)
            return {**inbox_update(state, result), 'settled': False}

        def route_settle(state: AgentState) -> Literal['finish', 'collect_inbox']:
            """只允许已确认关闭的候选答案结束，其他情况进入下一轮收信。"""
            return 'finish' if state['settled'] else 'collect_inbox'

        def finish(state: AgentState) -> dict[str, Any]:
            """返回最后一轮流式文本作为回答，并保留是否执行过工具。"""
            reply = state["messages"][-1]
            answer = str(reply.get("content") or "").strip()
            handled = bool(answer)
            LOGGER.info("event=tool_finish used_tools=%s round=%s", state["used_tools"], state["round_index"])
            return {"answer": answer or None, "handled": handled}

        builder = StateGraph(AgentState)
        builder.add_node('collect_inbox', collect_inbox)
        builder.add_node("call_model", call_model)
        builder.add_node("execute_tools", execute_tools)
        builder.add_node("finish", finish)
        builder.add_node('settle_answer', settle_candidate)
        builder.add_edge(START, 'call_model')
        builder.add_edge('collect_inbox', 'call_model')
        builder.add_conditional_edges(
            "call_model",
            route_model,
            {"execute_tools": "execute_tools", "settle_answer": "settle_answer"},
        )
        builder.add_conditional_edges('execute_tools', route_tools,
                                      {'collect_inbox': 'collect_inbox', 'call_model': 'call_model'})
        builder.add_conditional_edges('settle_answer', route_settle,
                                      {'finish': 'finish', 'collect_inbox': 'collect_inbox'})
        builder.add_edge("finish", END)
        graph = builder.compile()
        raw_messages = [*(context_messages or []), *messages]
        initial: AgentState = {
            "messages": raw_messages,
            "tool_calls": [],
            "round_index": 0,
            "used_tools": False,
            "tool_failures": {},
            "document_calls": {},
            "answer": None,
            "handled": False,
            'inbox_deliveries': [],
            'settled': False,
            'watching_inbox': False,
        }
        # thread_id 使用当前回答来源消息，后续接入持久化 checkpointer 时无需改变调用身份。
        return cast(AgentState, graph.invoke(initial, {
            "configurable": {"thread_id": str(checkpoint.message_id)},
            "recursion_limit": self.maximum_rounds * 3 + 4,
        }))
