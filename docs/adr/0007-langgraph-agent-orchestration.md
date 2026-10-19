# ADR 0007: 使用 LangGraph 编排对话智能体

Status: Partially superseded by ADR 0008 for web search routing and approval.

## 背景

项目此前在 `MemoryTools.prepare` 中用固定循环手写模型决策、工具执行和回传流程。该实现能工作，但节点、路由、执行上限和后续暂停恢复没有统一的编排模型。

## 决策

应用使用 LangGraph `StateGraph` 作为对话智能体编排层。完整回答图先准备长期记忆和附件全文材料，再进入统一的流式工具循环：每次模型调用都以 `stream=true` 接收文字和工具调用分片，工具执行后把结果追加到上下文并继续模型调用，最后一轮文字直接形成用户回答。网页来源解析和消息终态仍由统一收尾节点持久化。同步聊天和流式聊天共享同一执行路径，只由运行上下文选择即时写入还是聚合成同步结果。附件全文方案更新见 [ADR 0009](0009-conversation-attachment-full-context.md)。

LangGraph 负责节点状态与路由。Mem0 继续作为独立长期记忆服务，业务 PostgreSQL 继续保存来源、授权、版本、模型用量和外部副作用的幂等事实。业务 PostgreSQL 中的外部操作记录负责副作用幂等；LangGraph 官方 PostgreSQL checkpointer 负责图节点检查点、暂停与恢复。两类状态职责不同，均不写入 Mem0 PostgreSQL。

完整回答图的 `thread_id` 使用助手回答尝试标识；重试产生新的回答尝试和新的图身份。联网搜索已由 ADR 0008 改为普通模型工具，不再使用外发确认中断。图检查点位于业务 PostgreSQL 的独立 `langgraph` schema；会话删除时同步清理对应检查点。

## 结果

工具流程成为明确的 `call_model -> execute_tools -> call_model` 条件循环，并由 `finish` 节点结束；其中 `call_model` 消费真实 SSE，文字分片立即输出，`tool_calls` 分片按索引累积。完整回答流程成为 `route_request -> prepare_context -> agent -> finalize` 条件图。附件全文在回答准备阶段读取；联网搜索作为模型工具在工具循环中自主调用。同步和流式入口不再各自复制回答准备、异常收尾和终态持久化。

LangGraph 检查点只保存路由标志和失败状态，不复制模型密钥、回答正文、附件正文或长期记忆正文。正文和外部副作用事实仍只保存在各自业务存储中；应用重启会终止无法安全续接的模型网络请求。联网搜索已改为普通工具，不再依赖确认中断恢复。
