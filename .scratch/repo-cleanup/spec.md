# Repository cleanup and documentation refresh

Status: ready-for-agent

Completion: complete（2026-10-23）。

验证记录：[Repository cleanup verification](../../docs/verification-repository-cleanup.md)。

## 目标

在不改变当前产品行为的前提下，删除已失去调用方的代码、数据库字段和旧 checkpoint schema，并更新仍把旧搜索、附件或执行流程写成现行能力的文档。

## 清理边界

- 历史迁移文件和 verification 记录作为事实档案保留，不重写既有时间线。
- 数据库删除通过新迁移执行，不直接修改已应用迁移；删除前备份将移除的数据。
- 仅删除能通过引用搜索、测试和真实 schema 确认的遗留结构。
- 当前会话、账号、知识库文档、模型配置和有效来源数据保留。

## 已确认清理项

- 删除搜索确认流程遗留的 `search_requests.revision`、`purpose`、`expires_at`。
- 删除只写不读的 `messages.model_revision`、`messages.embedding_revision`。
- 删除视觉描述早期结构化输出遗留的 `document_images.ocr_text`、`uncertainties`；保留并写入 `caption`。
- 删除不再写入或恢复的 `langgraph` checkpoint schema。
- 删除无调用方的 `EmbeddingConfiguration.verified`、`OperationService.has_calls`、`ConversationGraph.delete_threads`、`AuthService.revoke_all_sessions` 和未使用的嵌入版本比较方法。
- 删除前端没有 DOM/组件引用的旧样式选择器。
- 更新 DeepSeek Search / Fetch、图注、知识引用、LangGraph 无检查点和当前附件工具文档。

## 验证

使用公开应用接口和真实 PostgreSQL 测试迁移、图注持久化、来源删除和对话执行。前端执行类型检查、无未使用符号检查和生产构建；完整 Python 回归作为最终门槛。
