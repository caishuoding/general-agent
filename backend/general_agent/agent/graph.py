"""使用 LangGraph 编排模型决策与业务工具执行。"""

import json
import logging
from collections.abc import Callable
from typing import Any, Literal, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from general_agent.agent.state import AgentState
from general_agent.agent.tools import ORCHESTRATION
from general_agent.infrastructure.calls import BoundCalls
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.infrastructure.settings import StoredSettings
from general_agent.memory.automatic import MemoryCheckpoint
from general_agent.prompts import tool_result_fragment, tool_turn_fragment


ToolExecutor = Callable[[MemoryCheckpoint, str, dict[str, Any], StoredSettings], dict[str, Any]]
ToolResultImages = Callable[[list[dict[str, Any]]], list[ModelMessage]]
LOGGER = logging.getLogger(__name__)
DOCUMENT_TOOLS = {'search_documents', 'read_document', 'search_knowledge'}


class AgentToolGraph:
    """构建并运行 LangGraph 工具循环，同时复用现有业务工具和幂等检查点。"""

    def __init__(self, maximum_rounds: int = 12, maximum_tool_retries: int = 5) -> None:
        """设置回答轮数和每个工具的重试上限；重试次数不包含首次失败调用。"""
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
    ) -> AgentState:
        """运行工具循环；模型及工具执行前可核验独立身份，编排结果不走普通缓存。"""

        def call_model(state: AgentState) -> dict[str, Any]:
            """组装当前消息并隐藏耗尽额度的工具，所有工具选择均由模型决定。"""
            if before_model is not None:
                before_model(state["messages"], tools)
            index = state["round_index"]
            if index >= self.maximum_rounds:
                raise ValueError("工具调用达到本次执行上限；已完成的操作保留，请缩小范围后继续。")
            key = checkpoint.key("tools:round", [settings.revision, index, state["messages"]])
            reply = checkpoint.get(key)
            if reply is None:
                fragments = [tool_turn_fragment(), *(context_fragments or [])]
                if any(message.get("role") == "tool" for message in state["messages"]):
                    fragments.append(tool_result_fragment())
                model_messages = [
                    {"role": "system", "content": "\n\n".join(fragments)},
                    *state["messages"],
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

        def route_model(state: AgentState) -> Literal["execute_tools", "finish"]:
            """根据模型是否返回函数调用选择执行工具或结束工具流程。"""
            return "execute_tools" if state["tool_calls"] else "finish"

        def execute_tools(state: AgentState) -> dict[str, Any]:
            """检查调用与重试限额；时间实时执行，文档读取预算包含同批已产生的结果。"""
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
                tool_key = checkpoint.key("tools:result", [settings.revision, state["round_index"] - 1, call])
                attempt_key = checkpoint.key("tools:attempt", [str(assistant_message_id), tool_key])
                # 当前时间每次执行都重新读取，不复用历史步骤中的旧时钟。
                cacheable = name != 'get_current_time' and name not in ORCHESTRATION
                result = checkpoint.get(attempt_key) if cacheable else None
                if result is None and cacheable:
                    cached = checkpoint.get(tool_key)
                    if cached is not None and "error" not in cached and cached.get("status") != "failed":
                        result = cached
                exhausted = failures.get(name, 0) > self.maximum_tool_retries
                if exhausted:
                    # 即使模型忽略可用工具列表，后端也不得继续执行已禁用工具。
                    result = {"error": "该工具已达到最大重试次数，本次回答中已禁用。"}
                elif result is None:
                    if name not in known_tools:
                        result = {"error": "未知工具，不允许执行。"}
                    else:
                        try:
                            result = execute_tool(checkpoint, name, arguments, settings)
                        except (ValueError, LookupError, ModelFailure) as error:
                            result = {"error": str(error)}
                if name in known_tools and ("error" in result or result.get("status") == "failed"):
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
            LOGGER.info("event=tool_execute names=%s", [call.get("function", {}).get("name") for call in state["tool_calls"]])
            return {
                "messages": [*state["messages"], *tool_messages],
                "tool_calls": [],
                "used_tools": True,
                "tool_failures": failures,
                "document_calls": document_calls,
            }

        def finish(state: AgentState) -> dict[str, Any]:
            """返回最后一轮流式文本作为回答，并保留是否执行过工具。"""
            reply = state["messages"][-1]
            answer = str(reply.get("content") or "").strip()
            handled = bool(answer)
            LOGGER.info("event=tool_finish used_tools=%s round=%s", state["used_tools"], state["round_index"])
            return {"answer": answer or None, "handled": handled}

        builder = StateGraph(AgentState)
        builder.add_node("call_model", call_model)
        builder.add_node("execute_tools", execute_tools)
        builder.add_node("finish", finish)
        builder.add_edge(START, "call_model")
        builder.add_conditional_edges(
            "call_model",
            route_model,
            {"execute_tools": "execute_tools", "finish": "finish"},
        )
        builder.add_edge("execute_tools", "call_model")
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
        }
        # thread_id 使用当前回答来源消息，后续接入持久化 checkpointer 时无需改变调用身份。
        return cast(AgentState, graph.invoke(initial, {
            "configurable": {"thread_id": str(checkpoint.message_id)},
            "recursion_limit": self.maximum_rounds * 2 + 2,
        }))
