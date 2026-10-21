# 对话图 State 数据流重构验证

日期：2026-10-21。决策见 [ADR 0012](adr/0012-state-based-conversation-flow.md)，规格见 [State 共享重构](../.scratch/langgraph-state-sharing/spec.md)。

## 实现结果

- ConversationState 显式保存 messages、source_attachment_ids、reserved_call_id、answer、failure、used_tools 和 web_sources。
- prepare_context 返回准备好的消息、材料来源与预留；agent 返回工具循环后的完整消息、回答及标志；finalize 返回来源核验后的消息、回答与来源。节点返回字典由 LangGraph 更新 State，不再修改 runtime.context。
- ConversationContext 使用冻结 dataclass，只保留固定身份、私密模型配置、操作身份和同步／流式模式。历史消息改为图的初始 State 输入。
- 流式回调保留本节点缓冲及已有 UI 增量写入，节点失败时将部分正文返回 State。外层消费框架的 values 流，边界异常使用最新 State 收尾。
- 消息以完整快照覆盖，工具历史可在外层 State 中观察；网页核验更新最后一条回答，不重复追加历史或修改旧快照。
- 当前外层不写 PostgreSQL 检查点，移除其连接池、启动步骤和未使用的 langgraph-checkpoint-postgres 依赖。业务持久化与停止／重试规则保留；旧检查点仍按会话来源身份清理。

## 验证

- 核心回归：101 项通过，覆盖 app、conversation、attachments、记忆工具、来源删除、LangSmith 模型追踪及联网搜索。
- 账号和启动回归：`pytest tests/auth -q --maxfail=2`，14 项通过。
- 最终图节点验证：8 项通过（不含已在核心回归中执行的旧检查点隔离清理场景）。覆盖真实图更新、context 不可变、部分流式失败、图边界失败及本地 LangSmith 节点输入输出。
- `mypy backend tests --no-incremental`：107 个文件检查通过。
- `uv lock --check --offline` 与 `git diff --check` 通过。

真实 LangSmith SDK 在本地收集追踪树，关闭网络投递；验证 prepare_context 输出可见准备材料、agent 读取相同 State、回答可见，节点输入输出不含模型配置密钥。数据库测试使用隔离 PostgreSQL 库，模型在 HTTP 边界采用受控响应。

本次不新增中断恢复、压缩或附件处理策略，也没有进行生产部署。附件真实使用、RAG 质量和长文章总结仍按 [附件待办](../.scratch/conversation-document-tools/TODO.md) 保持待验／待设计，不以本次回归替代。
