# 任务 09：忘记长期记忆

> 历史记录说明（2026-10-19）：下文保留当时的实现、审查和验收事实。当前联网搜索由模型根据用户请求按需自主调用并直接执行，无需“联网搜索：”等固定句式；包含附件或长期记忆中的个人信息也不增加确认。显式触发、查询规划/编辑/批准、确认卡片和搜索确认恢复流程均已移除。现行行为以 [ADR 0008](adr/0008-tool-driven-web-search-and-prompt-composition.md) 和 [当前联网说明](../README.md#模型自主联网与来源核验) 为准；下文旧流程及测试数字不代表当前版本验收。

## 当时的实现与验收记录

## 已实现的行为

管理页“忘记这条记忆”调用 `DELETE /api/memories/{action_id}`。完整成功返回 200，包含 `status=complete` 和“原聊天仍保留”说明；不存在返回 404；SDK 清理失败返回 503，保留 `deleting` 条目及安全错误，可点击“重试清理记忆”。重复删除相同标识幂等，不根据正文误删其他条目。

聊天同步 `/messages` 与浏览器 `/runs` 共用 `ChatExecution`。支持 `删除记忆：完整当前事实`、`忘记记忆：完整当前事实` 或 `删除记忆 UUID` / `忘记记忆 UUID`。全文事实必须唯一；不明确、批量或混合写作请求只澄清。`memory_delete_requests` 将原用户消息与具体目标绑定，失败重试沿原身份继续。删除不申请模型/嵌入预算、不要求当前嵌入验证，零预算也可进行。

接受删除的事务先将动作置为 `deleting`、清业务正文、递增失效版本，并取消依赖操作、清派生副本。随后取得与保存/更正相同的动作锁。保存、更正、候选的迟到成功或异常回写都不能覆盖删除墓碑。完整清理后才置 `deleted` 并从管理列表隐藏；清理中断或应用重启不重新启用事实。

原消息与历史助手回答的 `messages.content` 按 ADR 保留，可回看，也可在用户明确查旧聊天时读取。忘记记忆不会声称清除了原文。原附件保留；普通新会话不会自动读旧聊天或附件。用户用新消息再次明确保存是新的授权，不属于旧来源恢复。

## 真 Mem0 删除契约

使用锁定 `mem0ai==2.2.1` 的真实 SDK。删除时按 `embedding_indexes.personal_memories` 保存的地址、模型和维度打开无云端调用权限的会话，不使用当前密钥发请求；Mem0 history 由官方 SQLiteManager 管理。

1. 同时核对业务 `memory_id` 和向量 metadata 的 `action_id + user_id=local-person`，覆盖 SDK 向量已提交而业务尚未获得标识的部分成功。
2. 在调用 SDK 前把发现的标识写入 `memory_deletions.memory_ids`。只有身份匹配时才调用真实 `Memory.delete`。
3. 只有身份匹配时才调用真实 `Memory.delete`，并核验 `get` 不存在及该动作向量集合为空；不直接操作 Mem0 内部 history。
4. 向量删除失败时按持久标识重试，不把“向量不存在”误判为其他条目已删除。全部完成后清内部业务标识列表，仅保留无正文完成状态。

删除不增加云端用量；已发生的保存、更正、召回、回答与搜索费用保留。

## 15 可复用的公共边界与清理顺序

`MemoryDeletion.delete(action_id: UUID)` 是完整条目删除边界。外部调用方必须传已明确选中的业务动作标识；不要直接删业务行、只删向量或绕过本边界批量按事实全文删除。需要本地状态时可调用 `result(action_id)`，不返回事实正文。`clear_dependents(connection, action_id)` 是内部同事务清理步骤，不能独立替代正式 SDK 清理。

相关数据与失效规则：

- `memory_actions` 保留动作 ID、原来源 ID、操作 ID、版本及 `deleting/deleted` 状态，`fact=''`，完成后 `memory_id=NULL`。`memory_deletions` 是可恢复进度，无事实正文。
- `memory_source_suppressions` 只保存旧用户消息 UUID，无正文且无消息 FK。原来源、全部更正来源及受影响候选来源不能再次自动提取。`MemoryService.source_suppressed` 只用于“发现”入口；不能全局阻止同源其他独立候选的确认。
- 精确候选按 `target_id`、`memory_action_id` 及 `uuid5(candidate_id,'confirmed-memory')` 关联，覆盖确认的部分写入。清 `fact/source_quote/target_fact`、批准时间，置 `expired`；不删除同源其他正式条目或其他候选。`attachment_memory_candidates` 只删除这些候选对应的派生来源行，不删附件本体。14 已合入：`AttachmentMemory.prepare` 在外发前及提交事务内核验旧来源抑制；保留 `validate_source → session(before_write) → PgMemory/MeteredEmbedding` 的嵌入后、正式发布前来源守卫。
- 全部 `memory_updates` 清 `fact/previous_fact/error` 并置 `deleted`，旧更新重试拒绝；不只处理最后一个更新或只处理当前 operation。
- 操作闭包包含动作当前操作、原来源与更正来源的全部回答尝试、全部 update/candidate/discovery operations、包含动作的 recall checkpoints、引用动作的 search requests，以及依赖 document job 的所有回答尝试。
- 对闭包操作设 `cancelled/invalidated=TRUE`（区别于普通停止），清 `result/response_status/approved/pause_id/reason`，撤销未开始调用；活跃回答停止并清预算暂停。真实已发生调用不删。`BudgetService.invalidate` 和 `invalidate_conversation` 同样清暂停挑战，供来源删除复用。
- 从 `memory_recalls.matches` 移除该动作。检查点只含标识/版本；记录时与有效版本和操作状态原子核验，防止清理之后写回旧依赖。
- 搜索记录置 `expired` 并清 `query/results/sources/purpose`，撤销有效期；对应 `messages.web_sources` 清空。旧搜索确认、预算批准和恢复接口均拒绝，迟到搜索结果不得重新缓存。
- 依赖召回的 `document_jobs` 清 `final_content/final_call_id/context_hash`，对应消息清派生引用与进度。文档分段从未接收记忆材料，故保留 `document_steps` 与原文件。最终结果发布与删除失效原子核验，删除后迟到回答不会恢复缓存。

15 删除原消息/会话时还必须处理本票增加的 `memory_delete_requests.message_id` FK、`memory_deletions.action_id` FK，以及此前全部 memory action/update/candidate/recall/discovery 的来源 FK。对用户选择保留的记忆，应断开来源关系、标记来源已删除，并改现有来源 INNER JOIN 为可显示缺失来源的逻辑；该功能属于 15，本票未提前实现。`memory_source_suppressions` 可作为最小防恢复身份继续保留。

16 的历史查询引用不缓存正文，指向本次明确保留的原消息；本票不删除这些合法历史来源。15 删除源消息时才按 16 合同清来源引用及所有关联操作。

## 验证记录

公开 HTTP 为主要验证边界，真实 PostgreSQL 17 / pgvector 和 Mem0，外部模型与嵌入仅在 HTTP 边界使用受控响应；故障在真实数据库历史边界注入，不替换应用内部模块。独立随机测试数据库位于 15433，全批次使用共享 `serialized-test.py`，未操作主库、主服务、真实密钥或共享容器重启。

纵向红灯：未实现 DELETE 为 405；旧候选重复确认错误返回 200；明确聊天删除被预算错误阻挡为 402。逐项实现后通过。浏览器首次因原会话标题与事实相同导致选择器匹配两条，已改为匹配具体事实元素，不改变产品行为。

已完成专项覆盖：指定条目删除/重复删除、零预算、原聊天及其他记忆跨重启保留；同源候选精确删除与兄弟确认；sync/runs 明确删除；真实历史 purge 失败、重启和恢复；SDK 部分保存按动作身份清除；在途更正删除与迟到结果；搜索旧批准和预算挑战失效；总结最终缓存及派生引用清理、原附件分段保留。必要持久化核验用于证明隐藏 SDK 历史与派生正文实际清除，不能仅凭界面缺失推定删除。

验证批次：

- 合 16 前，删除、保存、候选、更正、召回、全文及搜索共 62 项：61 项先通过，1 项发现普通停止与删除不应共用缓存失效规则。新增 `cloud_operations.invalidated` 区分删除失效，修复后该项与删除缓存项 2 项通过；未无故重复其余已绿项。
- 合 16 后，8 项删除 + 7 项旧聊天查询 + 普通停止缓存契约共 16 项通过（17.94 秒）。补充主动查保留原聊天与旧更正幂等缓存拒绝的 2 项复验通过（3.19 秒）。
- 浏览器管理页忘记→刷新→原消息回看→零预算聊天删除 1 项通过；合 16 后删除和历史查询两专项再次通过。删除截图已查看，无遮挡或状态错误。
- 合 14 后已加入精确附件候选清理、外发前及提交事务内旧来源抑制，保留全部原来源守卫；附件同源另一候选仍可澄清/确认，原文件仍可下载。附件确认嵌入在途删除也纳入最终组合批次。
- 最终静态检查：完整 mypy 65 个源文件通过，TypeScript / Vite 构建通过，`git diff --check` 通过。

合 14 及最新集成 `433c819` 后：10 项删除、附件候选、直接保存、普通候选与 SDK 适配共 **43 项一次通过**（43.86 秒）；删除与附件人物确认浏览器 **2 项通过**（3.9 / 4.5 秒），构建通过。最终工作树保留 14 的来源回调、16 的只读历史边界，并核验集成分支为祖先。

集成复验：删除、附件候选、直接保存、历史与总结停止/失效共 40 项公开接口测试通过（41.54 秒），mypy 65 个源文件及前端类型检查通过。默认 Playwright 配置补充排除 `deletion.spec.ts`，由已接入 `test:e2e` 的专用删除配置运行；`--list` 核对默认 8 项不含删除、专用配置仅含删除 1 项，避免使用错误的模型替身。

真实云端嵌入/模型与 Tavily 组合验收：**待验**。用户已同意本轮先完成实现；受控替身通过不等于真实服务兼容、语义质量或用户配置已经验收。数据库重启无需重启共享测试服务；本票验证应用重启与持久化恢复。
