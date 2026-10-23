# Repository cleanup verification

日期：2026-10-23。

## 删除项

- 迁移 051 删除旧搜索确认字段 `search_requests.revision/purpose/expires_at`。
- 迁移 051 删除只写不读的 `messages.model_revision/embedding_revision`。
- 迁移 051 删除旧图片结构化描述字段 `document_images.ocr_text/uncertainties`；保留 `caption` 并在附件入库时写入。
- 迁移 052 删除不再写入或恢复的 `langgraph` checkpoint schema。
- 删除无调用方的 `EmbeddingConfiguration.verified`、`OperationService.has_calls`、`ConversationGraph.delete_threads`、`AuthService.revoke_all_sessions` 和未使用的嵌入版本比较方法。
- 删除前端没有 DOM/组件引用的旧导航、退出、模型状态和管理详情样式。
- 更新 DeepSeek Search / Fetch、当前附件工具、知识库图注/引用、无检查点执行和测试边界文档。

## 保留项

- 历史迁移与既有 verification 文档保留为事实记录。
- 会话、账号、模型配置、当前知识库文档、消息和有效调用记录保留。
- `document_images.caption`、来源位置和实际调用版本记录继续用于当前功能。
- 无法证明无用的表或字段不删除。

## 验证

- 完整 Python 回归：238 passed；一条 pypdf 既有弃用警告。
- MyPy：132 个文件通过。
- 前端 TypeScript、无未使用局部变量/参数检查和生产构建通过。
- 浏览器默认配置 14 项、图片 1 项、搜索 1 项、来源删除 1 项、手机键盘 3 项，共 20 项通过。
- 当前数据库确认迁移 051/052 已应用，遗留字段与 `langgraph` schema 均不存在，知识库文档保留。
- 删除前数据备份：`/tmp/general-agent-cleanup-backup-20261023-131641`。
