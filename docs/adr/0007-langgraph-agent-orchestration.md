# ADR 0007: 使用 LangGraph 编排对话智能体

Status: Partially superseded by ADR 0008 for web search routing and approval; request routing removed on 2026-10-20.

2026-10-21：节点间数据传递与外层检查点策略由 [ADR 0012](0012-state-based-conversation-flow.md) 替代。当前节点通过 State 共享结果，不再修改 runtime.context 或新增外层 PostgreSQL 检查点；下文相关段落保留为历史决策。

## 背景

项目此前在 `MemoryTools.prepare` 中用固定循环手写模型决策、工具执行和回传流程。该实现能工作，但节点、路由、执行上限和后续暂停恢复没有统一的编排模型。

## 决策

应用使用 LangGraph `StateGraph` 作为对话智能体编排层。完整回答图先准备长期记忆、有效文档清单和图片 file_id，再进入统一的流式工具循环：每次模型调用都以 `stream=true` 接收文字和工具调用分片，工具执行后把结果追加到上下文并继续模型调用，最后一轮文字直接形成用户回答。网页来源解析和消息终态仍由统一收尾节点持久化。同步聊天和流式聊天共享同一执行路径，只由运行上下文选择即时写入还是聚合成同步结果。文档正文按 [ADR 0011](0011-conversation-document-tools.md) 由模型通过工具按需读取。

2026-10-20，用户明确要求删除模型前的人工意图判断。移除关键词识别记忆操作后隐藏图片的规则，以及只服务于该规则的 `route_request`、`route_after_request`、材料中转缓存和重复错误路由。所有有效图片均以 file_id 提供给模型，不依据用户消息措辞筛选；工具选择仍由模型决定。准备失败时不发模型请求，由统一收尾保存失败状态。账号权限、附件归属与有效期、工具参数、资源限额、来源失效和幂等检查保留。

LangGraph 负责节点状态与路由。Mem0 继续作为独立长期记忆服务，业务 PostgreSQL 继续保存来源、授权、版本、模型用量和外部副作用的幂等事实。业务 PostgreSQL 中的外部操作记录负责副作用幂等；LangGraph 官方 PostgreSQL checkpointer 负责图节点检查点、暂停与恢复。两类状态职责不同，均不写入 Mem0 PostgreSQL。

完整回答图的 `thread_id` 使用助手回答尝试标识；重试产生新的回答尝试和新的图身份。联网搜索已由 ADR 0008 改为普通模型工具，不再使用外发确认中断。图检查点位于业务 PostgreSQL 的独立 `langgraph` schema；会话删除时同步清理对应检查点。

## 结果

工具流程成为明确的 `call_model -> execute_tools -> call_model` 条件循环，并由 `finish` 节点结束；其中 `call_model` 消费真实 SSE，文字分片立即输出，`tool_calls` 分片按索引累积。完整回答流程为 `prepare_context -> agent -> finalize`。文档通过工具读取，联网搜索作为模型工具在工具循环中自主调用。同步和流式入口不再各自复制回答准备、异常收尾和终态持久化。

外层 LangGraph 检查点只保存失败状态，不复制模型密钥、回答正文、附件正文或长期记忆正文。正文和外部副作用事实仍只保存在各自业务存储中；应用重启会终止无法安全续接的模型网络请求。联网搜索已改为普通工具，不再依赖确认中断恢复。
