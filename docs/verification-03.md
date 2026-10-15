# 任务 03 验收记录

日期：2026-10-15。工作分支：`codex/ticket-03-capabilities`。完成范围为模型能力验证与独立嵌入配置。

## 产品行为

- 保留聊天 `/api/settings` 的公开响应兼容性。嵌入使用独立的 `/api/settings/embedding` GET/PUT 与 `embedding.json`，字段包括地址、模型、密钥配置状态和预期维度。
- 显式复用聊天连接会复制地址与凭据，不会假定聊天模型提供嵌入接口，也不会在之后自动联动两套连接。
- `/api/settings/capabilities` GET 返回当前配置版本的分项状态；POST `/api/settings/capabilities/{text|image|tools|embedding}` 单独执行一项检查。失败作为检查结果持久化，未配置时返回 409 且不发请求。
- 文字检查要求非空回答。图片检查发送真实 16×16 红色 PNG，要求正确识别颜色；工具检查要求工具调用标识、函数名和参数有效；嵌入检查要求响应模型等于配置值、条数与索引正确、每个向量维度正确且值为有限数值。图片检查仅代表基础图像协议可用。
- 配置 revision 在地址、模型、密钥或嵌入维度实际改变时更新；相同配置保存保留 revision。结果绑定检查时的快照，迟到的旧检查不验证新配置。界面也忽略跨配置变更迟到的检查读取。
- 能力调用通过 `ModelClient` 外部 HTTP 边界，记录每次已知用量或未知字段。服务错误正文不回传，认证失败提供固定可解释信息。每个测试按钮均明确提示可能计费。
- 存在检索索引时，地址、模型、维度变更返回 409 并保留旧配置；轮换密钥允许。重建功能由任务 18 提供，本任务没有实现重建或假装索引已更新。

## 后续消费者接口

`EmbeddingConfiguration(database, EmbeddingSettingsStore(directory))` 是配置和索引身份协调接口：

- `verified()` 返回当前验证通过的 `StoredEmbeddingSettings`，未通过或仅有旧版本结果时抛出可公开的 `ValueError`。
- `bind_index(name, saved)` 在 PostgreSQL advisory lock 下检查当前版本并登记索引名称与 `(base_url, model, dimensions)`。检索消费者应先获取已验证配置，再登记索引后初始化或写入向量。禁止跳过登记直接创建检索集合。
- 配置保存与登记使用相同锁。后续重建流程需协调更新 `embedding_indexes` 和实际索引，不能仅更改此登记表冒充重建完成。
- `ModelClient.embed(saved, texts)` 返回 `Embedding`，其中 `model`、`dimensions`、`data`（含 `index`/`embedding`）与 `usage` 可供 Mem0 Adapter 使用。返回顺序由 `index` 解释；调用者应按索引排序。该方法仅负责外部协议，后续统一预算模块应包裹实际调用与计量。
- 验证调用目前通过 `CapabilityService` 写入 `model_calls`，`conversation_id` 为空，`purpose` 为 `capability_text`、`capability_image`、`capability_tools` 或 `capability_embedding`。票 04 应将此边界接入统一预算策略，避免重复计量。

迁移 `004_capabilities.sql` 增加能力结果与索引登记，并让不属于会话的调用可记录用量。已有会话不删除。

## 受控测试

测试 Seam 为应用 HTTP API；使用随机独立 PostgreSQL 数据库，外部 HTTP 由 `httpx.MockTransport` 或本机 HTTP 替身控制。没有使用 SQLite、内部模块 mock 或真实配置。已有索引场景使用数据库前置数据建立尚未实现的消费者状态，最终结果仍通过 HTTP 断言。

已执行 red → green：独立嵌入接口最初 404；四项检查最初 404；非法向量被错误接受；缺少索引登记表；缺少工具调用标识被错误接受；浏览器最初缺少嵌入表单。逐项修正后验证通过。

- `PYTHONPATH=backend .venv/bin/pytest -q`：22 项通过。任务新增 7 项覆盖独立配置持久化、部分成功/失败、用量、认证、模型/维度/向量异常、并发旧结果、索引变更保护、工具结构、无变化保存与嵌入二次校验脱敏。
- `PYTHONPATH=backend .venv/bin/mypy`：通过，16 个 Python 文件。
- `pnpm --dir frontend build`：TypeScript 检查与 Vite 构建通过。
- `pnpm --dir frontend test:e2e`：会话 2 项及能力设置 1 项，分别使用隔离服务。能力流程覆盖四项检查、嵌入复用、部分失败、模型更新失效、刷新持久化与空密钥输入。

现有 FastAPI TestClient 对 httpx 的弃用提示为依赖告警，不影响测试结果。测试结束仅删除测试自身创建的数据库；未重启共享数据库或触碰主应用。

## 真实服务验收

文字、图片、工具调用、嵌入四项均为 **待验**：任务 03 没有读取真实 `.local`、使用云端凭据或发出真实请求。任务 01 的文字验收不替代本任务的分项能力验收。受控测试通过仅证明应用按协议响应工作，不宣称任何真实服务已支持这些能力。
