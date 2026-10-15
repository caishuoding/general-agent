# 任务 04 验收记录与调用接口

日期：2026-10-15。分支：`codex/ticket-04-budget`。基于 `82e86fa`，完成前已合入 `bb2bff2`。实现范围为用量、费用估算、月度预算与一次调用批准；没有连接真实付费服务或修改主应用数据库。

## 产品行为与数据

- 设置页管理可选月度预算及当前聊天、嵌入模型单价；金额统一为 **CNY 人民币元**，token 单价为 **元 / 百万 token**。其他币种需用户先换算。月度统计以 **Asia/Shanghai 自然月**划分，查询全部记录，与最近 100 条列表无关。
- 预算留空允许正常使用；0 表示每次调用前暂停。预算已达到、当前调用缺所需单价、本月已有费用未知或尚有调用等待用量时，阻止新的调用并返回 HTTP 402。估算不是服务商实际账单或扣费上限，一次已允许的请求仍可能超出预算。
- 暂停提示可打开预算设置、重新检查、取消，或明确批准当前操作的**下一次云端调用**。批准不覆盖整次多轮执行，也不覆盖以后操作。缺失 usage 无法补算，用户可单次批准或关闭预算；补齐缺失单价可补算已有完整用量。
- 价格绑定精确的 `(base_url, model)`，调用保存配置 revision 与单价快照；换地址或模型不沿用其他身份的价格。已知费用保留原价格，补价只处理此前未知费用。嵌入只使用输入 token；其缺失 completion_tokens 仍在明细显示未知，但不阻止输入费用估算。
- `Decimal` / PostgreSQL NUMERIC 计算，费用公开为十进制字符串。逐字段保留可用 usage，缺失或非法字段保持 NULL。流的 usage 尾帧更新同一调用，停止与失败不重复记账；业务内容校验失败不会回滚已记录用量。
- 同步聊天、流式启动、重试及四项能力检查均经过 `CallGateway`。没有 SDK 隐藏重试或不计量的能力按钮。停止冻结文字，但已发出的请求继续收集可返回用量；重启会终结遗留生成及已停止待用量状态，并保留未知费用。
- 迁移 `005_budget.sql` 新建 `budget_settings`、`model_prices`、`cloud_operations`，扩展 `model_calls`。预算、批准、调用与操作缓存均在 PostgreSQL；凭据继续在既有本机配置文件中。旧调用没有服务地址/完整价格依据时保留未知，不将历史费用伪造为零。

## HTTP 接口

| 接口 | 行为 |
| --- | --- |
| `GET/PUT /api/settings/budget` | `monthly_limit: decimal string or null`；`prices[]` 每项含 base_url、model、input_per_million、output_per_million，可扩展 per_credit。GET 返回 currency 和 timezone。PUT 为完整价格列表替换。 |
| `GET /api/usage` | 最近 100 次实际调用；含 operation_id、purpose、base_url、model、unit、可空 token/credits 与 estimated_cost。 |
| `GET /api/usage/summary` | 当月 calls、known_cost、estimated_cost（任一未知则 NULL）、unknown_calls、pending_calls、month、currency、timezone、monthly_limit。 |
| `POST /api/budget/operations/{id}/approve` | body 必须带当前 `pause_id`。只能批准尚有效的暂停挑战；不发送云端请求。 |
| `POST /api/budget/operations/{id}/cancel` | 撤销操作后续调用和批准；不会撤销已发生费用。 |

所有有云端副作用的现有 POST 接受 `X-Operation-ID: UUID`。前端为每次新操作生成 UUID，重试同一暂停时复用原 UUID、路由和请求正文。服务器把用途、会话、内容/目标和配置版本的范围指纹持久化；相同 UUID 换内容、会话、能力或模型配置返回 409。未传 UUID 的兼容客户端由服务器生成；402 中返回生成后的标识，恢复时必须使用它。

402 的公开结构包含 `code=budget_paused`、`operation_id`、`pause_id`、`reason`、`detail`、`approval_scope=next_call`，不包含请求正文、密钥或外部服务原始错误。原因值为 `budget_reached`、`missing_price`、`unknown_cost`、`in_flight`。成功/模型失败结果被持久化用于幂等重放；同一标识重复提交不重复发请求。若实际调用已经开始但结果缓存尚未确认，返回 409 要求查看原执行，不能因不明结果自动重发。

## 后续 Mem0、资料、工具与搜索的公共接入合同

公共入口在 `general_agent.budget` 与 `general_agent.calls`。应用实例也暴露 `app.state.budget` 与 `app.state.calls`。`ModelClient` 仅是网关内部原始协议边界；新增业务不能直接使用它或默认 SDK 客户端。

1. 用 `BudgetService.operation(purpose, scope, operation_id=None, conversation_id=None)` 取得 `(OperationContext, previous_operation)`。scope 必须由后端根据实际内容/目标、配置版本及业务来源身份构造；不可直接信任客户端提交的任意 scope。用途可为 memory_save、memory_recall、attachment_embed、web_search 等。个人单用户身份由应用固定，会话可空。
2. `OperationContext` 是不可变数据：operation_id、purpose、conversation_id。将 `CallGateway.bind(context)` 返回的**操作专属 BoundCalls 实例**绑定在该次 SDK 的 embedder/llm 上，并将同一实例显式传入工作线程。**不能仅用 ContextVar；Mem0 的内部线程池不会继承它。不得在共享 Memory 实例上临时替换适配器造成串线。**
3. `bound.complete(settings, messages)`、`bound.probe(settings, capability)`、`bound.embed(embedding_settings, texts)` 每次实际 HTTP 前 reserve/begin，返回文本/向量之前记录 usage。批量 embed 是一个真实 HTTP，按一次记录。适配器只从已计量结果取文本或向量，随后 SDK 数据写入失败不会回滚计量。
4. 捕获 `BudgetPaused` 并保存业务执行步骤。其 `public()` 可直接作为 402 返回。用户批准必须调用上面的带 pause_id 接口，再使用**同一个操作 ID、范围和业务检查点**恢复；只继续尚未发出的下一步，不从头重放已成功嵌入/写入。批准在 reserve 短事务中原子消耗，下一轮调用会再次检查并产生新 pause_id。旧批准不能被重放。
5. `budget.cancel(context)` 撤销后续调用；之后 reserve/begin 拒绝，已发出请求仍可以 record。业务层还要停止继续编排步骤，不能把取消异常吞掉后另开操作绕过。
6. 当前流式 `start/retry` 只有一个最终聊天调用，因此启动路由提前 reserve，再把 `context + call_id` 显式传给后台线程。**后续在回答前增加 Mem0/资料嵌入时，必须把预留移到“下一次真正发送的调用”并持久化步骤；不可先占用最终聊天预留后再尝试嵌入，否则启用预算时会被 in_flight 正确阻止。** 04 未实现未来多步骤编排或后台暂停恢复界面；05/06/资料票应在各自业务状态机实现，不复用当前仅适合单调用路由的 `perform` 从头重跑逻辑。
7. `bound.call(settings, unit, reserved=None, assistant_message_id=None)` 是非 token 服务的公开计量包围器。unit 可为 chat_tokens、embedding_tokens、credits。搜索适配器可在此上下文内发送一次受控 HTTP，第一时间解析可用 credits 并调用 `budget.record(call_id, TokenUsage(), credits=Decimal(...))`；然后再解析业务正文。缺失用量传 None，失败由包围器保存未知状态。credits 单价是同身份价格记录中的 `per_credit`，单位元 / credit。搜索功能本身仍由 17 实现。
8. 关闭 SDK 自动重试。每次确实重发必须重新进入一个 call，形成新的调用记录；同一次 HTTP 的进度/尾帧用原 call_id 更新，不追加记录。任何取得用量后发生的业务错误都不能删除计量行。

简化的显式绑定示例（配置应先由现有 `EmbeddingConfiguration.verified()` 取得）：

```python
with budget.operation("memory_save", immutable_scope, operation_id, conversation_id) as (context, previous):
    calls = gateway.bind(context)
    # 把 calls 显式绑定在这一次 SDK 的嵌入适配器实例；线程内使用同一实例。
    embedding = calls.embed(verified_settings, [approved_fact])
    # 再执行 SDK/业务写入；其失败不撤销已经记录的外部用量。
```

**并发与锁：** reserve 使用全局 PostgreSQL 短事务 advisory lock，原子读取预算、检查当月全表、消耗批准并保存 reserved 记录；在途记录不能被当成零。网络等待不持有这个锁，停止接口可立即写状态。相同 operation_id 的请求由独立 session advisory lock 串行防重复；同会话事务使用 NO KEY UPDATE，允许独立计量事务的外键检查。应用仍按既有约定单后端进程运行。

**缓存与删除合同：** `cache_result(context, result, status)` 可持久化应用的幂等公开结果，但不自动解决 SDK 部分提交。后续 09/15 删除记忆/来源必须在业务删除状态下调用 `invalidate(operation_id)` 或 `invalidate_conversation(conversation_id)`，并在重放前确认目标仍有效。失效会清除缓存正文、撤销批准并标记取消；并发晚到的 cache_result 不会写回已取消操作。跨会话/非会话来源仍需业务保存其关联 operation_id，并逐一失效。调用计量行保留，不含请求/回答正文。未来删除 conversation 本体时也需处理现有外键关系，不能只删除用户消息却保留缓存正文。

## 验证证据

按已确认 Seam 使用真实 PostgreSQL 随机数据库、公开 HTTP API、受控外部 HTTP 响应及公开适配器契约。没有内部模块 mock 或 SQLite。月份测试仅用 SQL 安排两条记录在上海月初两侧的时间夹具，所有验收断言来自 HTTP；其余业务通过应用接口建立。

TDD 红灯记录：预算设置最初 404；部分 usage 的无效字段会抹掉有效字段；预算界面最初缺少表单；价格表异步刷新重置正在填写的预算；已停止请求在进程强制终止后永久保留 pending；操作外键使不存在会话误报内部异常。分别修复后绿灯。第一次旧锁模式的测试等待被终止，修复为兼容独立计量外键检查的行锁后通过。

- 完整 Python：`PYTHONPATH=backend .venv/bin/python -m pytest -q`，39 项通过。
- 类型检查：`PYTHONPATH=backend .venv/bin/python -m mypy backend tests`，23 个文件通过。
- 前端：`pnpm --dir frontend build`，TypeScript 与 Vite 通过。
- 浏览器：`TEST_APP_PORT=18086 TEST_MODEL_PORT=18087 TEST_PYTHON=... pnpm --dir frontend test:e2e`，原聊天/流式 5 项、能力 1 项、预算 1 项，共 7 项通过。独立临时配置与测试库，端口仅 18086/18087。查看过预算页面截图，没有遮挡或溢出。
- 新增覆盖：预算等于阈值、一次批准与指纹校验、重复提交、取消、缺失价格与部分 usage、嵌入业务校验失败仍计量、模型变化不继承价格、补价与重启、101 条全月统计、上海月初边界、并发在途暂停、停止尾帧、重试重新检查、SDK 线程池显式上下文与逐次批准、生成/已停止状态下真实进程终止恢复。
- 所有新增/修改 Python 函数都有中文 docstring；新增前端函数与回调有中文用途注释。`git diff --check` 通过。

## 真实服务验收与限制

真实云端单价、服务实际 usage、账单对应、真实流式停止费用均 **待验**。本任务没有读取/复制主 `.local` 或凭据、发起付费请求、重启共享 PostgreSQL、改动主库或部署主应用。按用户已确认口径，缺凭据真实验收可保留待验，不以受控测试冒充服务兼容或实际账单准确。

本次成本按输入/输出 token 或 credits 的显式单价估算；缓存 token、分层价格、折扣、税费、汇率变动等服务商特殊规则未建模。价格应由用户根据服务实际规则填写；usage 缺失时保持未知。已有历史记录无服务身份不能安全补价，预算启用后可按次批准或关闭限制。多步骤 Mem0/资料业务、搜索和删除缓存集成由相应后续任务验收。
