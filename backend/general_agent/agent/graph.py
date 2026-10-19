"""使用 LangGraph 编排模型决策与业务工具执行。"""

import json
import logging
from collections.abc import Callable
from typing import Any, Literal, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from general_agent.agent.state import AgentState
from general_agent.infrastructure.calls import BoundCalls
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.infrastructure.settings import StoredSettings
from general_agent.memory.automatic import MemoryCheckpoint
from general_agent.prompts import tool_result_fragment, tool_turn_fragment


ToolExecutor = Callable[[MemoryCheckpoint, str, dict[str, Any], StoredSettings], dict[str, Any]]
LOGGER = logging.getLogger(__name__)


class AgentToolGraph:
    """构建并运行 LangGraph 工具循环，同时复用现有业务工具和幂等检查点。"""

    def __init__(self, maximum_rounds: int = 12) -> None:
        """设置单次执行允许的最大模型决策轮数，防止工具循环失控。"""
        self.maximum_rounds = maximum_rounds

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
    ) -> AgentState:
        """运行统一流式工具循环，边接收文字边输出，工具调用分片累积后执行。"""

        def call_model(state: AgentState) -> dict[str, Any]:
            """组装当前原始消息并调用模型生成工具请求或最终回答。"""
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
                    tools=tools,
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
                    if not isinstance(json.loads(call["function"]["arguments"]), dict):
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
            """顺序执行模型请求的白名单工具，并把结构化结果回传给下一模型节点。"""
            tool_messages: list[dict[str, Any]] = []
            for call in state["tool_calls"]:
                function = call["function"]
                arguments = json.loads(function["arguments"])
                tool_key = checkpoint.key("tools:result", [settings.revision, state["round_index"] - 1, call])
                result = checkpoint.get(tool_key)
                if result is None:
                    try:
                        result = execute_tool(checkpoint, function["name"], arguments, settings)
                    except (ValueError, LookupError, ModelFailure) as error:
                        # 业务拒绝也作为工具观察返回，让模型有机会修正参数或停止操作。
                        result = {"error": str(error)}
                    checkpoint.put(tool_key, result)
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })
            LOGGER.info("event=tool_execute names=%s", [call.get("function", {}).get("name") for call in state["tool_calls"]])
            return {
                "messages": [*state["messages"], *tool_messages],
                "tool_calls": [],
                "used_tools": True,
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
            "answer": None,
            "handled": False,
        }
        # thread_id 使用当前回答来源消息，后续接入持久化 checkpointer 时无需改变调用身份。
        return cast(AgentState, graph.invoke(initial, {
            "configurable": {"thread_id": str(checkpoint.message_id)},
            "recursion_limit": self.maximum_rounds * 2 + 2,
        }))
