# 任务 18：嵌入空间重建

## 产品与公开入口

设置页保存不同地址、模型或维度时先展示目标和影响清单，明确确认后受理后台重建。实际向量范围只有正式长期记忆；附件文字、全文总结、图片理解与历史消息均不新增向量索引，也不提供数据迁移工具。同空间凭据轮换直接保存并重新验证。

- `POST /api/settings/embedding/rebuild-impact`：沿嵌入配置输入返回目标、有效记忆数量及各集合 affected；不外发请求。
- `POST /api/settings/embedding/rebuilds`：`{id: UUID, target: EmbeddingSettingsInput, confirmed: true}`，返回202和稳定任务；相同身份/目标复用，改变目标409，未明确确认409。只允许一个未结束任务。
- `GET /api/settings/embedding/rebuilds/latest` 和 `GET .../{id}`：实际状态、阶段、活动/目标模型维度、total/completed/excluded、可查询状态、预算挑战和安全错误，不返回密钥或事实正文。
- `POST .../{id}/resume`：继续原目标，逐步核对已落目标行；不是重新开始整批。`POST .../{id}/cancel`：先撤回后续调用，目标物理清理完成才显示 cancelled。publishing/cleanup 已越过发布提交点，取消返回409，只能继续完成发布/清理。
- paused 的 `pause.operation_id/pause_id` 仍通过原 `/api/budget/operations/{id}/approve` 只批准下一次调用，然后 resume。也可先补预算/单价再直接 resume。

阶段为 initializing → verifying → embedding → publishing → cleanup → complete，取消路径为 cancelling → cancelled。status 为 running/paused/failed/complete/cancelling/cancelled。新空间未完整可用时普通聊天明确降级，新的记忆保存、更正及候选确认在接受新版本前拒绝为可恢复维护状态；管理查看和忘记仍可用。cleanup 阶段新索引已可查询，旧表清理失败不回退活动配置。

## 有效事实与稳定身份

初始化屏障等待已经在途的完整业务发布后，只快照 `memory_actions.status='complete' AND memory_id IS NOT NULL` 的当前版本；不要求来源会话、消息或附件仍存在。15明确保留的 `source_deleted=TRUE`、原 message_id/conversation_id 为 NULL 的事实继续有效。未确认候选、删除状态、历史旧值和失败动作不进入目标。

如果屏障之后仍存在 pending/saving/failed 保存或更正，任务诚实失败，保持原索引，提示取消重建后先恢复原操作或明确忘记条目。尤其不能排除已暂停更正、清旧表后让原 stable memory_id 永久无法恢复。deleting 条目虽不参加目标，但退休表必须等唯一记忆删除边界完成清理之后才能移除，保留部分 SDK 写入按 action_id 对账的线索。

`PgMemory.write_rebuilt` 使用锁定的真实 Mem0 1.0.11/PGVector，仅向受控目标表 insert/update 既有 memory_id 和向量；不调用 `Memory.add`、`Memory.update` 或 `reset`，不新增事实历史事件。已存在行先核对 action/user/job/version/正文。业务 action_id、memory_id、version、时间不变；真实 SDK 创建/修改时间及更正元信息沿用原 payload，正文仍以当前批准事实为准。原向量缺失时可从有效业务事实修复，不从历史恢复。来源已删除标记保留并移除 SDK 失效消息/会话链接。

发布前再用真实 SDK 检查当前有效集合逐条完整存在、ID/版本/身份/正文一致，并按最新来源状态清失效链接。步骤计数本身不代表目标已完整可用。目标缺行会失败，显式恢复只重嵌缺失事实，完整目标行直接复用。

## 数据、私有配置与恢复

迁移 `019_embedding_rebuild.sql`：

- `embedding_spaces`：受控物理名、地址/模型/维度、active/target/retired/removed。首次将原 personal_memories 登记为初始活动集合，以后目标名固定为 memory_space_<内部UUID>；只枚举本应用登记，不扫描任意数据库表。
- `embedding_rebuilds`：目标/旧空间ID、配置revision、阶段/状态、范围摘要、暂停与安全错误。不含完整密钥或事实副本。
- `embedding_rebuild_steps`：任务、动作、正式记忆ID、版本、事实摘要、状态、当前 operation/call ID。另 `embedding_rebuild_operations` 保留每次尝试的全部操作关联。不存事实正文、完整嵌入响应或候选副本。

目标配置通过现有0600临时文件与原子替换写 `APP_DATA_DIR/embedding-rebuilds/<job UUID>.json`，应用自己的目录按0700创建；目标不会提前覆盖活动配置。配置接口不回显密钥，错误与日志不输出请求正文。任务完成/取消后移除目标文件。活动配置仍为 `APP_DATA_DIR/embedding.json`。

发布在空间独占租约内先持久 publishing 门槛，再原子替换活动配置文件，最后一笔DB事务切换活动空间、逻辑 embedding_indexes 身份及 cleanup 阶段。文件已换而DB未换时保持维护，向量消费者不能盲用新配置查询旧表；查看/删除使用登记的活动空间和无外发权限的SDK。恢复 publishing 核对同一私有目标和向量再幂等完成切换，不依赖文件修改时间猜测状态。当前沿用文件级 fsync+replace，不把进程恢复证据冒充断电/目录 fsync 验证。

新空间发布后才删除退休表。DROP因本机依赖等原因失败时，活动新空间仍可用，任务 failed/cleanup 如实保留，resume 仅重试本地清理，不再次嵌入。完整数据库已发布后目标文件丢失，cleanup 可核对已发布配置revision完成清理。发布之前目标私有文件不可读则保持失败，需要恢复对应本机文件或在提交点前取消。

应用启动在通用调用恢复之前标记 running/cancelling 任务为 failed，保留阶段、完整目标步骤及有效暂停挑战，不自动发云请求。任务锁串行 resume；已提交目标、进度尚未提交时先按目标 payload 对账。响应未交给应用且目标尚未写入时，上一调用真实/未知用量保留，用户明确恢复后才重试；不能保证这种中断没有重复费用。正常退出等待本任务工作线程，仍按整个应用的单后端进程约定运行。

## 锁序、预算与删除公共合同

原91402705仅保护SDK构造，不能被当作业务锁。本票新增 `MemorySpaces.lease`，91402718共享 session advisory lock 覆盖完整保存、更正、候选确认及召回检查点发布；SDK session 使用可重入租约，避免同一业务嵌套取锁与排队独占锁形成自锁。新业务在排队前及取锁后核验维护门槛。候选/动作锁均在外层空间租约内，发布不等待候选或动作 advisory lock。

独占屏障用短间隔 `pg_try_advisory_lock` 等待，而不是把独占请求排在慢云请求之后阻止新的共享租约。这样15预览/接受能先取共享租约，再按会话→附件→动作行锁做本地SDK核验，接受删除依然可在旧云请求返回前停用来源。独占发布仅本地核验、文件与DB操作，不持它调用外部服务。更正已经在途时等待其完整发布再快照，不能只等待SDK写入结束。

每个探针/事实步骤单独持久操作、reserve、begin、计量，scope绑定job/step/版本/目标revision；每次批准仍只释放下一次调用。SDK部分失败与重试不回滚实际用量。步骤操作登记与job取消在同一job行锁顺序下串行，取消会撤回所有已登记尝试的许可；响应回来后再次核对job、action有效版本和操作，禁止迟到写入。

09 `MemoryDeletion` 仍是唯一完整删除边界，15只继续复用 begin/delete：

1. begin 在原短事务立即 tombstone、清业务事实/所有原有派生正文，取消相关步骤全部 operation；新增清 `embedding_rebuild_steps.fact_hash` 并置 invalidated，取消步骤所有尝试、旧pause/result/approved。步骤只留最小ID/版本墓碑，不清无关事实的步骤。
2. 随后共享空间租约→同一action advisory lock，逐个枚举所有未removed的active/target/retired空间。既按memory_id，也按SDK metadata的action_id/user_id对账部分提交，先持久化发现ID，再逐空间删除并核验。
3. 所有登记空间副本及共享Mem0历史purge确认后才报告 complete；不能只在未来发布时过滤action。空间清理失败维持删除失败状态，事实仍停止召回。删除后目标迟到结果拒绝发布，后续重建将该步骤排除。
4. 原聊天按09/ADR保留；15来源删除的原文清理由15负责。18不新增从旧来源恢复的入口，source_deleted保留事实继续参与有效索引。

## 验证记录

按既定TDD公开HTTP seam逐项红绿；真实 PostgreSQL/pgvector15433与真实Mem0，只有外部HTTP受控。故障通过真实数据库触发器、目标行删除、依赖视图与独立进程强杀制造，未mock内部记忆或重建模块。

- `tests/memory/test_rebuild.py` 14项：地址/模型/维度变更、同维度换模型、同模型换维度、同空间密钥轮换、稳定ID/来源/SDK时间历史、明确保留来源已删事实、未确认候选、逐步预算/刷新与重启、SDK部分提交、文件与DB发布间隙、目标行丢失、在途更正/来源删除/忘记/取消、未完成更正恢复、退休清理失败及跨空间删除。
- `tests/memory/test_rebuild_restart.py` 1项：真实uvicorn进程在第二条嵌入中强杀，重启不自动外发，完整第一条复用，仅重做中断请求，未知用量保留。未重启共享数据库。
- 任务18与来源删除/记忆删除/更正/召回/Mem0/能力组合56项一次通过（65.68秒）。新增未完成更正/退休清理边界后，含2新增、2并发复验和保存/候选/附件确认的35项一次通过（38.79秒）。两批覆盖89个不同公开/适配用例，不声称两批是同一次全套执行。
- 严格mypy73文件通过，前端tsc/Vite构建通过；修改Python函数AST用途注释审计无缺失，前端新增回调逐个补中文说明。
- 浏览器3项分别通过：重建流程（7.2秒）覆盖预览、失败取消、逐次预算批准、刷新恢复和完整发布；能力配置（2.4秒）、来源删除（6.2秒）验证共享设置与删除界面仍可用。已检查重建预算暂停截图；后续浏览器批次按工具默认规则清理临时截图。

真实云端嵌入服务的模型/维度兼容、语义相关性和真实账单仍待验。本票没有读取主 `.local` 或真实密钥，没有调用付费服务、修改主服务/数据库或push；按用户决定完成实现与受控验收，不将替身结果冒充真实服务验收。
