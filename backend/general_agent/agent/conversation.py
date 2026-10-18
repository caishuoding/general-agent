"""用 LangGraph 统一编排同步与流式对话的完整执行链。"""

from dataclasses import dataclass
import logging
import re
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb

from general_agent.attachments.service import AttachmentContext, AttachmentService
from general_agent.conversation.execution import AnswerPreparation, ChatExecution
from general_agent.conversation.history import HistoryContext
from general_agent.conversation.summary import SummaryPreparation, summary_mode
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.infrastructure.settings import StoredSettings
from general_agent.search.service import SearchAwaiting

LOGGER = logging.getLogger(__name__)


class ConversationState(TypedDict):
    """保存一次回答尝试的图状态；外部依赖和密钥不进入状态。"""

    has_local_answer: bool
    requires_context: bool
    failure: str | None
    paused: bool


class ConversationResult(TypedDict):
    """向聊天入口返回最终结果，不把回答正文复制进 LangGraph 检查点。"""

    answer: str
    failure: str | None
    paused: bool


def memory_tool_request(content: str) -> bool:
    """识别只应交给记忆工具判断的明确记忆操作，避免随消息附件被误当作回答资料读取。"""
    return bool(re.search(r'(?:请)?(?:记住|忘记)|(?:更新|修改|更正|删除|查找|搜索|查询).{0,8}(?:长期)?记忆', content))


@dataclass
class ConversationContext:
    """向图节点提供本次执行依赖和不可持久化运行参数。"""

    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    settings: StoredSettings
    operation: OperationContext
    reserved_call_id: UUID | None
    rows: list[dict[str, Any]]
    delivery: Literal["sync", "stream"]
    preparation: AnswerPreparation | None = None
    routed_history: HistoryContext | None = None
    routed_external: list[ModelMessage] | None = None
    routed_material: AttachmentContext | None = None
    routes_prepared: bool = False
    answer: str = ""


class ConversationGraph:
    """以一个小接口隐藏完整对话编排、输出解析和消息终态持久化。"""

    def __init__(self, database: Database, calls: CallGateway, execution: ChatExecution, attachments: AttachmentService) -> None:
        """绑定领域模块并只编译一次状态图，后续每次回答复用同一图定义。"""
        self.database = database
        self.calls = calls
        self.execution = execution
        self.attachments = attachments
        self.documents = execution.documents
        builder = StateGraph(ConversationState, context_schema=ConversationContext)
        builder.add_node("route_request", self.route_request)
        builder.add_node("agent", self.agent)
        builder.add_node("prepare_context", self.prepare_context)
        builder.add_node("generate_answer", self.generate_answer)
        builder.add_node("finalize", self.finalize)
        builder.add_edge(START, "route_request")
        builder.add_conditional_edges(
            "route_request",
            self.route_after_request,
            {"agent": "agent", "prepare_context": "prepare_context", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "agent",
            self.route_after_agent,
            {"prepare_context": "prepare_context", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "prepare_context",
            self.route_after_prepare,
            {"generate_answer": "generate_answer", "finalize": "finalize"},
        )
        builder.add_edge("generate_answer", "finalize")
        builder.add_edge("finalize", END)
        self.builder = builder
        self.checkpoint_pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] | None = None
        self.checkpointer: PostgresSaver | None = None
        self.compiled = builder.compile(name="conversation")

    def start(self) -> None:
        """初始化官方 PostgreSQL 检查点并让完整回答图具备持久化执行状态。"""
        if self.checkpoint_pool is not None:
            return
        with self.database.connect() as business_connection:
            # 框架检查点与业务表分 schema，避免 LangGraph 内部表混入 public 业务模型。
            business_connection.execute("CREATE SCHEMA IF NOT EXISTS langgraph")
        pool = cast(
            ConnectionPool[psycopg.Connection[dict[str, Any]]],
            ConnectionPool(
                conninfo=self.database.url,
                min_size=1,
                max_size=4,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                    "connect_timeout": 5,
                    "options": "-c search_path=langgraph",
                },
            ),
        )
        try:
            pool.open(wait=True)
            saver = PostgresSaver(pool)
            saver.setup()
            self.checkpoint_pool = pool
            self.checkpointer = saver
            self.compiled = self.builder.compile(checkpointer=saver, name="conversation")
        except BaseException:
            pool.close()
            raise

    def close(self) -> None:
        """关闭图检查点专用连接，不影响业务连接和独立 Mem0 服务。"""
        if self.checkpoint_pool is not None:
            self.checkpoint_pool.close()
            self.checkpoint_pool = None
            self.checkpointer = None

    def delete_threads(self, thread_ids: list[UUID]) -> None:
        """删除已清除消息对应的图检查点，避免业务来源删除后遗留无用执行元数据。"""
        if self.checkpointer is None:
            return
        for thread_id in thread_ids:
            self.checkpointer.delete_thread(str(thread_id))

    def run(self, context: ConversationContext) -> ConversationResult:
        """运行一次完整回答图；调用方只需登记消息并选择同步或流式传输。"""
        initial: ConversationState = {
            "has_local_answer": False,
            "requires_context": False,
            "failure": None,
            "paused": False,
        }
        return self.invoke(initial, context)

    def resume(self, context: ConversationContext) -> ConversationResult:
        """从同一回答标识的 LangGraph 检查点恢复已获用户决定的联网步骤。"""
        return self.invoke(Command(resume=True), context)

    def invoke(self, graph_input: ConversationState | Command[Any], context: ConversationContext) -> ConversationResult:
        """统一调用图并兜住检查点或节点边界外异常，保证消息不会永久停在生成中。"""
        LOGGER.info("event=graph_start conversation_id=%s message_id=%s delivery=%s", context.conversation_id, context.assistant_message_id, context.delivery)
        try:
            raw = cast(dict[str, Any], self.compiled.invoke(
                graph_input,
                {"configurable": {"thread_id": str(context.assistant_message_id)}},
                context=context,
            ))
            paused = bool(raw.get("__interrupt__")) or bool(raw.get("paused", False))
            LOGGER.info("event=graph_end message_id=%s failure=%s paused=%s", context.assistant_message_id, bool(raw.get("failure")), paused)
            return {"answer": context.answer, "failure": raw.get("failure"), "paused": paused}
        except Exception:
            LOGGER.exception(
                "LangGraph 对话执行失败 conversation_id=%s message_id=%s",
                context.conversation_id,
                context.assistant_message_id,
            )
            detail = self.unexpected_failure(context.delivery)
            self.fail_terminal(context, detail)
            return {"answer": context.answer, "failure": detail, "paused": False}

    def route_request(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """先处理明确联网、历史、附件和文档总结路由，避免工具规划模型抢先误判。"""
        context = runtime.context
        content = str(context.rows[-1]["content"])
        try:
            history = self.execution.history.prepare(
                context.conversation_id,
                context.user_message_id,
                content,
                context.operation,
            )
            external = None if history else self.execution.search.prepare(
                context.conversation_id,
                context.user_message_id,
                content,
                context.settings,
                context.operation,
            )
            context.routed_history = history
            context.routed_external = external
            context.routes_prepared = True
            attachment_scope = self.attachments.question_scope(context.conversation_id, context.user_message_id, content, [])
            material = AttachmentContext()
            memory_request = memory_tool_request(content)
            if history is None and external is None and summary_mode(content) is None and not memory_request:
                material = self.attachments.prepare(
                    context.conversation_id,
                    context.user_message_id,
                    content,
                    settings=context.settings,
                    only_attachment_ids=attachment_scope,
                )
            context.routed_material = material
            # 明确记忆操作优先进入工具图；随消息选择的附件只能作为消息归属，不能因此被读入保存流程。
            requires_context = bool(history or external or summary_mode(content) is not None)
            if not memory_request and (attachment_scope is not None or material.messages):
                requires_context = True
            LOGGER.info(
                "event=node route_request message_id=%s history=%s external=%s material=%s memory_tool=%s requires_context=%s",
                context.assistant_message_id,
                history is not None,
                external is not None,
                bool(material.messages),
                memory_request,
                requires_context,
            )
            return {"requires_context": requires_context}
        except SearchAwaiting:
            # 先把业务消息标为暂停，再交由 LangGraph 保存断点；恢复时本节点会重新核验已批准内容。
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE messages SET status='paused' WHERE id=%s AND status='generating'",
                    (context.assistant_message_id,),
                )
            interrupt({"reason": "search_approval"})
            return {}
        except (ValueError, ModelFailure) as error:
            detail = str(error)
            if context.delivery == "sync":
                context.answer = detail
            return {"failure": detail}
        except Exception:
            LOGGER.exception(
                "LangGraph 请求路由失败 conversation_id=%s message_id=%s",
                context.conversation_id,
                context.assistant_message_id,
            )
            detail = self.unexpected_failure(context.delivery)
            if context.delivery == "sync":
                context.answer = detail
            return {"failure": detail}

    def route_after_request(self, state: ConversationState) -> Literal["agent", "prepare_context", "finalize"]:
        """显式资料请求直接准备上下文，路由失败直接收尾，其余请求进入工具智能体。"""
        if state["failure"]:
            return "finalize"
        return "prepare_context" if state["requires_context"] else "agent"

    def agent(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """执行 LangGraph 工具子图，得到工具直接回答或进入资料回答阶段。"""
        context = runtime.context
        content = str(context.rows[-1]["content"])
        try:
            answer = self.execution.prepare(
                context.conversation_id,
                context.user_message_id,
                content,
                context.operation,
                context.reserved_call_id,
                context.settings,
            )
            context.answer = answer or ""
            LOGGER.info("event=node agent message_id=%s handled=%s", context.assistant_message_id, answer is not None)
            return {"has_local_answer": answer is not None}
        except SearchAwaiting:
            return {"paused": True}
        except (ValueError, ModelFailure) as error:
            detail = str(error)
            if context.delivery == "sync":
                context.answer = detail
            return {"failure": detail}
        except Exception:
            LOGGER.exception(
                "LangGraph 工具节点失败 conversation_id=%s message_id=%s",
                context.conversation_id,
                context.assistant_message_id,
            )
            detail = self.unexpected_failure(context.delivery)
            if context.delivery == "sync":
                context.answer = detail
            return {"failure": detail}

    def route_after_agent(self, state: ConversationState) -> Literal["prepare_context", "finalize"]:
        """工具已回答、暂停或失败时直接收尾，否则继续准备全部回答材料。"""
        return "finalize" if state["has_local_answer"] or state["paused"] or state["failure"] else "prepare_context"

    def prepare_context(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """准备长期记忆、附件、历史和文档总结材料。"""
        context = runtime.context
        try:
            preparation = self.execution.prepare_response(
                context.conversation_id,
                context.user_message_id,
                context.assistant_message_id,
                context.settings,
                context.operation,
                context.reserved_call_id,
                context.rows,
                routed_history=context.routed_history,
                routed_external=context.routed_external,
                routed_material=context.routed_material,
                routes_prepared=context.routes_prepared,
            )
            context.preparation = preparation
            context.answer = preparation.local_answer or ""
            LOGGER.info("event=node prepare_context message_id=%s local_answer=%s", context.assistant_message_id, preparation.local_answer is not None)
            return {"has_local_answer": preparation.local_answer is not None}
        except SearchAwaiting:
            return {"paused": True}
        except (ValueError, ModelFailure) as error:
            detail = str(error)
            if context.delivery == "sync":
                context.answer = detail
            return {"failure": detail}
        except Exception:
            LOGGER.exception(
                "LangGraph 上下文准备失败 conversation_id=%s message_id=%s",
                context.conversation_id,
                context.assistant_message_id,
            )
            detail = self.unexpected_failure(context.delivery)
            if context.delivery == "sync":
                context.answer = detail
            return {"failure": detail}

    def route_after_prepare(self, state: ConversationState) -> Literal["generate_answer", "finalize"]:
        """缓存回答、暂停和失败无需调用最终模型，其余状态进入生成节点。"""
        return "finalize" if state["has_local_answer"] or state["paused"] or state["failure"] else "generate_answer"

    def generate_answer(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """按入口选择同步完成或逐段输出，并复用文档总结的最终步骤。"""
        context = runtime.context
        preparation = context.preparation
        if preparation is None:
            return {"failure": self.unexpected_failure(context.delivery)}
        summary = preparation.summary
        try:
            reserved = self.documents.reserve_final(summary, context.settings, context.operation) if summary else preparation.reserved
            if context.delivery == "sync":
                answer = self.calls.bind(context.operation).complete(
                    context.settings,
                    preparation.messages,
                    reserved,
                    context.assistant_message_id,
                ).content
            else:
                call_id = reserved or self.calls.operations.reserve(context.operation, context.settings)
                answer = ""
                for chunk in self.calls.bind(context.operation).stream(
                    context.settings,
                    preparation.messages,
                    call_id,
                    context.assistant_message_id,
                ):
                    answer += chunk.content
                    # 每段先留在运行上下文；即使随后断流，收尾节点仍能保留已收到的正文。
                    context.answer = answer
                    if chunk.content and (not preparation.history or self.execution.history.available(context.operation)):
                        with self.database.connect() as connection:
                            connection.execute(
                                "UPDATE messages SET content=content || %s WHERE id=%s AND status='generating'",
                                (chunk.content, context.assistant_message_id),
                            )
            if summary:
                answer = self.documents.finish(summary, answer)
            context.answer = answer
            LOGGER.info("event=node generate_answer message_id=%s characters=%s summary=%s", context.assistant_message_id, len(answer), summary is not None)
            return {}
        except (ValueError, ModelFailure) as error:
            detail = str(error)
            if context.delivery == "sync":
                context.answer = detail
            return {"failure": detail}
        except Exception:
            LOGGER.exception(
                "LangGraph 回答生成失败 conversation_id=%s message_id=%s",
                context.conversation_id,
                context.assistant_message_id,
            )
            detail = self.unexpected_failure(context.delivery)
            if context.delivery == "sync":
                context.answer = detail
            return {"failure": detail}

    def finalize(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """统一解析出处、保存终态，并确保同步与流式入口使用相同收尾规则。"""
        context = runtime.context
        preparation = context.preparation
        if state["paused"]:
            with self.database.connect() as connection:
                connection.execute("UPDATE messages SET status='paused' WHERE id=%s AND status='generating'", (context.assistant_message_id,))
            return {}
        failure = state["failure"]
        summary = preparation.summary if preparation else None
        if failure:
            self.execution.search.fail(context.operation, failure)
            if summary:
                self.documents.failed_final(summary)
        answer = context.answer
        history: HistoryContext | None = preparation.history if preparation else None
        material = preparation.material if preparation else AttachmentContext()
        answer = self.execution.history.resolve(context.operation, answer, bool(history and history.messages))
        answer, web_sources = self.execution.search.resolve(context.operation, answer)
        answer, citations = self.attachments.resolve(context.conversation_id, answer, material)
        context.answer = answer
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE messages SET content=%s,status=%s,error=%s,citations=%s,web_sources=%s WHERE id=%s AND status='generating'",
                (answer, "failed" if failure else "complete", failure, Jsonb(citations), Jsonb(web_sources), context.assistant_message_id),
            )
            connection.execute(
                "UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'",
                (context.reserved_call_id,),
            )
        LOGGER.info("event=node finalize message_id=%s status=%s characters=%s", context.assistant_message_id, "failed" if failure else "complete", len(answer))
        return {}

    def unexpected_failure(self, delivery: Literal["sync", "stream"]) -> str:
        """按传输方式返回不泄露内部异常的稳定失败说明。"""
        return "回答执行中断，已保留部分内容，可以重试。" if delivery == "stream" else "记忆操作未完成，可以重试核对。"

    def fail_terminal(self, context: ConversationContext, detail: str) -> None:
        """在图或检查点意外失败时保存可见正文和失败终态，并撤销未启动的预留调用。"""
        if context.delivery == "sync" and not context.answer:
            context.answer = detail
        try:
            self.execution.search.fail(context.operation, detail)
            if context.preparation and context.preparation.summary:
                self.documents.failed_final(context.preparation.summary)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE messages SET content=%s,status='failed',error=%s WHERE id=%s AND status IN ('generating','paused')",
                    (context.answer, detail, context.assistant_message_id),
                )
                connection.execute(
                    "UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'",
                    (context.reserved_call_id,),
                )
        except Exception:
            # 原始图异常优先返回；数据库自身不可用时由启动恢复流程处理残留状态。
            return
