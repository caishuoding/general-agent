"""用 LangGraph 统一编排同步与流式对话的完整执行链。"""

from dataclasses import dataclass
import logging
import re
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.runtime import Runtime
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb

from general_agent.attachments.service import AttachmentContext, AttachmentService
from general_agent.conversation.execution import AnswerPreparation, ChatExecution
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelFailure
from general_agent.infrastructure.settings import StoredSettings

LOGGER = logging.getLogger(__name__)


class ConversationState(TypedDict):
    """保存一次回答尝试的图状态；外部依赖和密钥不进入状态。"""

    failure: str | None


class ConversationResult(TypedDict):
    """向聊天入口返回最终结果，不把回答正文复制进 LangGraph 检查点。"""

    answer: str
    failure: str | None


def memory_tool_request(content: str) -> bool:
    """识别只应交给记忆工具判断的明确记忆操作，避免随消息附件被误当作回答资料读取。"""
    return bool(re.search(r'(?:请)?(?:记住|忘记)|(?:更新|修改|更正|删除|查找|搜索|查询).{0,8}(?:长期)?记忆', content))


@dataclass
class ConversationContext:
    """向图节点提供本次执行依赖和不可持久化运行参数。"""

    conversation_id: UUID
    account_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    settings: StoredSettings
    operation: OperationContext
    reserved_call_id: UUID | None
    rows: list[dict[str, Any]]
    delivery: Literal["sync", "stream"]
    preparation: AnswerPreparation | None = None
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
        builder = StateGraph(ConversationState, context_schema=ConversationContext)
        builder.add_node("route_request", self.route_request)
        builder.add_node("prepare_context", self.prepare_context)
        builder.add_node("agent", self.agent)
        builder.add_node("finalize", self.finalize)
        builder.add_edge(START, "route_request")
        builder.add_conditional_edges(
            "route_request",
            self.route_after_request,
            {"prepare_context": "prepare_context", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "prepare_context",
            self.route_after_prepare,
            {"agent": "agent", "finalize": "finalize"},
        )
        builder.add_edge("agent", "finalize")
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
            "failure": None,
        }
        return self.invoke(initial, context)

    def invoke(self, graph_input: ConversationState, context: ConversationContext) -> ConversationResult:
        """统一调用图并兜住检查点或节点边界外异常，保证消息不会永久停在生成中。"""
        LOGGER.info("event=graph_start conversation_id=%s message_id=%s delivery=%s", context.conversation_id, context.assistant_message_id, context.delivery)
        try:
            raw = cast(dict[str, Any], self.compiled.invoke(
                graph_input,
                {"configurable": {"thread_id": str(context.assistant_message_id)}},
                context=context,
            ))
            LOGGER.info("event=graph_end message_id=%s failure=%s", context.assistant_message_id, bool(raw.get("failure")))
            return {"answer": context.answer, "failure": raw.get("failure")}
        except Exception:
            LOGGER.exception(
                "LangGraph 对话执行失败 conversation_id=%s message_id=%s",
                context.conversation_id,
                context.assistant_message_id,
            )
            detail = self.unexpected_failure(context.delivery)
            self.fail_terminal(context, detail)
            return {"answer": context.answer, "failure": detail}

    def route_request(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """预先读取当前会话全部可用附件，联网则由后续模型工具自主决定。"""
        context = runtime.context
        content = str(context.rows[-1]["content"])
        try:
            context.routes_prepared = True
            material = AttachmentContext()
            memory_request = memory_tool_request(content)
            if not memory_request:
                material = self.attachments.prepare(context.conversation_id, context.account_id)
            context.routed_material = material
            LOGGER.info(
                "event=node route_request message_id=%s material=%s memory_tool=%s",
                context.assistant_message_id,
                bool(material.messages),
                memory_request,
            )
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

    def route_after_request(self, state: ConversationState) -> Literal["prepare_context", "finalize"]:
        """路由失败直接收尾，其余请求先准备完整上下文。"""
        if state["failure"]:
            return "finalize"
        return "prepare_context"

    def agent(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """运行统一流式工具循环，文字增量直接写入当前回答。"""
        context = runtime.context
        preparation = context.preparation
        if preparation is None:
            return {"failure": self.unexpected_failure(context.delivery)}
        content = str(context.rows[-1]["content"])

        def on_content(text: str) -> None:
            """把模型文字增量加入当前回答，供同步聚合或流式持久化。"""
            if not text:
                return
            context.answer += text
            if context.delivery == "stream":
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
                content,
                context.operation,
                context.reserved_call_id,
                context.settings,
                messages=preparation.messages,
                assistant_message_id=context.assistant_message_id,
                on_content=on_content,
                material=preparation.material,
            )
            if not outcome.answer and not context.answer:
                raise ValueError("模型没有返回最终回答。")
            context.answer = outcome.answer or context.answer
            LOGGER.info(
                "event=node agent message_id=%s used_tools=%s characters=%s",
                context.assistant_message_id,
                outcome.used_tools,
                len(context.answer),
            )
            return {}
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

    def prepare_context(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """准备长期记忆、完整附件资料和会话历史。"""
        context = runtime.context
        try:
            preparation = self.execution.prepare_response(
                context.conversation_id,
                context.account_id,
                context.user_message_id,
                context.assistant_message_id,
                context.settings,
                context.operation,
                context.reserved_call_id,
                context.rows,
                routed_material=context.routed_material,
                routes_prepared=context.routes_prepared,
            )
            context.preparation = preparation
            LOGGER.info("event=node prepare_context message_id=%s", context.assistant_message_id)
            return {}
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

    def route_after_prepare(self, state: ConversationState) -> Literal["agent", "finalize"]:
        """失败直接收尾，其余状态进入统一流式工具循环。"""
        return "finalize" if state["failure"] else "agent"

    def finalize(self, state: ConversationState, runtime: Runtime[ConversationContext]) -> dict[str, Any]:
        """统一核验网页来源并保存终态，使同步与流式入口使用相同收尾规则。"""
        context = runtime.context
        failure = state["failure"]
        if failure:
            self.execution.search.fail(context.operation, failure)
        answer = context.answer
        answer, web_sources = self.execution.search.resolve(context.operation, answer)
        context.answer = answer
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE messages SET content=%s,status=%s,error=%s,web_sources=%s WHERE id=%s AND status='generating'",
                (answer, "failed" if failure else "complete", failure, Jsonb(web_sources), context.assistant_message_id),
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
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE messages SET content=%s,status='failed',error=%s WHERE id=%s AND status='generating'",
                    (context.answer, detail, context.assistant_message_id),
                )
                connection.execute(
                    "UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status='reserved'",
                    (context.reserved_call_id,),
                )
        except Exception:
            # 原始图异常优先返回；数据库自身不可用时由启动恢复流程处理残留状态。
            return
