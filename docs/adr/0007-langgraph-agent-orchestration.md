# ADR 0007: 使用 LangGraph 编排对话智能体

Status: Accepted

## 背景

项目此前在 `MemoryTools.prepare` 中用固定循环手写模型决策、工具执行和回传流程。该实现能工作，但节点、路由、执行上限和后续暂停恢复没有统一的编排模型。

## 决策

应用使用 LangGraph `StateGraph` 作为对话智能体编排层。工具决策循环是独立子图；完整回答图统一编排工具子图、上下文准备、长期记忆召回、附件材料、文档总结、最终模型调用、引用解析和消息终态持久化。同步聊天和流式聊天共享同一张完整回答图，只由运行上下文选择传输方式。

LangGraph 负责节点状态与路由。Mem0 继续作为独立长期记忆服务，业务 PostgreSQL 继续保存来源、授权、版本、模型用量和外部副作用的幂等事实。业务 PostgreSQL 中的外部操作记录负责副作用幂等；LangGraph 官方 PostgreSQL checkpointer 负责图节点检查点、暂停与恢复。两类状态职责不同，均不写入 Mem0 PostgreSQL。

完整回答图的 `thread_id` 使用助手回答尝试标识；重试产生新的回答尝试和新的图身份。联网内容需要用户确认时使用 LangGraph `interrupt` 暂停，批准后使用 `Command(resume=...)` 从同一检查点继续。图检查点位于业务 PostgreSQL 的独立 `langgraph` schema；会话删除时同步清理对应检查点。

## 结果

工具流程成为明确的 `call_model -> execute_tools -> call_model` 条件循环，并由 `finish` 节点结束。完整回答流程成为 `route_request -> agent/prepare_context -> generate_answer -> finalize` 条件图。明确历史、联网、附件和文档请求先走确定性路由；联网能力不注册为模型工具，不能绕过外发确认。同步和流式入口不再各自复制回答准备、异常收尾和终态持久化。业务工具接口及前端 API 保持不变。

LangGraph 检查点只保存路由标志和失败状态，不复制模型密钥、回答正文、附件正文或长期记忆正文。正文和外部副作用事实仍只保存在各自业务存储中；应用重启会终止无法安全续接的模型网络请求，而等待用户批准的联网节点可从同一图检查点恢复。
