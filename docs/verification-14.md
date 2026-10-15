# 任务 14：附件候选、人物确认与正式记忆

## 已实现行为

当前消息明确要求“请从附件提取长期记忆”“请从附件 文件名.pdf 提取记忆”或“请记住这份附件里的信息”时，同步 `/messages` 与浏览器 `/runs` 进入同一个 `AttachmentMemory` 边界。优先读取本条实际发送的文件，其次是请求点名且此前已发送的文件，未指名时读取当前会话此前已发送的可用附件，最多五个。未发送、其他会话、失效或缺少原文件的来源不能替代成其他资料；仅上传、一般问答、否定指令和材料中的保存指令不会授权提取。直接“请记住：具体事实”继续沿用直接保存规则。

TXT/Markdown、文本 PDF、DOCX 使用现有可信文字片段与位置；PDF 页码及 DOCX 正文/表格位置由后端决定。PNG/JPEG 使用真实图片内容，只在实际需要读图时要求当前配置的图片能力已通过；未验证不会先发提取调用。模型只能引用提供的来源键，文字依据必须逐字包含在该片段内。图像可见依据需用户对照原图核对，不把图片占位文字当作读取结果。

提取使用现有相关片段选择（最多 12 个文字片段）及所选图片，最多提出五个候选；这不是全文穷尽提取。长资料的全文总结仍由任务 13 负责，文档扫描、嵌图及复杂排版限制沿用任务 11/12。回执明确标记本次相关材料范围。

所有附件候选初始人物都为“尚未核对”，模型人物提示只作参考。卡片展示具体事实、原文/图片依据、文件下载与可信位置。`PUT /api/memory-candidates/{id}/subject` 选择本人或填写具体他人，只生成“人物：…。事实：…”的精确预览；不得替换提取事实。人物未明确时后端拒绝确认，界面也禁用保存。之后使用既有逐项确认接口写入 Mem0，另一候选不会随同批准。正式记忆页面保留文件来源和批准人物。

拒绝、重复确认、发现重试与正式保存失败使用既有持久化候选/动作身份。提取和正式嵌入分别计入统一预算；仅允许下一次调用不会放行后续嵌入。预算暂停、重启、确认失败重放保持真实状态和单一正式身份。异步停止后的迟到提取不能发布候选。无正式批准前候选不会参与跨会话召回。

## 来源与删除/重建合同（供 09、15、18）

- `memory_candidates.source_kind` 新增 `attachment`，保留原来源用户消息、会话、`source_quote`、批准事实与正式动作关联。附件候选 ID 为 `uuid5(source_message_id, 'attachment-candidate:N')`；正式动作仍为 `uuid5(candidate_id, 'confirmed-memory')`。
- 新 `attachment_memory_candidates` 以 `candidate_id` 为主键：`attachment_id`、`source_version`（不可变上传的 created_at）、`citation_key`、`filename`、`location`、`extracted_fact`、`suggested_subject`、`subject_label`、`is_image`。其中姓名、文件名、事实、位置等是派生正文，不应只清正式事实而保留它们。删除具体候选的这一行可整体清理，不能因此删除同源的其他独立事实。
- `validate_attachment_source(database, directory, candidate, require_person=True)` 要求候选未拒绝/过期、附件 `status='ready'`、原会话相同、上传版本相同、请求消息之前已实际绑定、原文件存在、文字原文仍匹配可信片段，且人物已澄清。文件仍在磁盘但附件开始删除、状态不再 `ready` 时立即失效。任务 15 应先把来源设为不可用，再清派生数据；不能仅在最后 unlink 文件。
- `MemoryService.save(..., validate_source=...)` 在登记前、`MeteredEmbedding.embed` 实际嵌入返回并记账后（SDK 写入前）以及正式动作发布前调用该守卫。`MemoryService.session(..., before_write=...)` / `PgMemory(..., before_write=...)` 传递此回调；直接保存、更正、召回等不受附件守卫影响。失效抛出 ValueError，不能把失败标成已保存。未来重建已确认且明确保留的记忆不应重新要求已删除原文件存在。
- 来源删除还须协调所有发现/确认云操作和并发发布，取消对应 `memory_discovery_operations`、`memory_candidate_operations`、`memory_action_operations` 并采用任务 09 的动作墓碑与条件发布。来源检查不是代替删除锁/墓碑的全局互斥；任务 15 需要以来源失效和取消协调检查到正式写入之间的竞态，并清理已产生但未发布的 Mem0 向量与历史。
- 清理范围包括 `memory_candidates` 的 fact/source_quote/target_fact、上述附件来源表、发现/确认/正式动作相关 `cloud_operations.result` 与许可、受影响消息内容、Mem0 正文/向量/历史以及任务 13 的分段与汇总缓存。`scope_hash` 不含正文，保留其操作身份用途；不存在 `scope` 列。保留需要防止复活的最小 UUID/状态关系。仅删除附件且用户明确保留的已确认有效记忆，应由任务 15 脱离已删除来源后继续有效，并可由任务 18 重建。
- `AttachmentService.prepare(..., only_attachment_ids=...)` 是额外的获准范围过滤，不另造检索/执行引擎。附件正文从不作为当前用户输入传给任务 08 自动候选发现。

## 验证

开发按公开 HTTP 纵向红灯/绿灯推进：初始附件明确请求没有预览、图片重试入口拒绝、提取带入未选文件、确认期间丢失来源仍成功，均先以测试复现再修复；“这份附件”指代及缺少原文件边界同样有公开行为回归。浏览器先观察原卡片错误显示“归属：你”，再实现人物澄清。

- 新 `tests/test_attachment_memory.py`：真实 PostgreSQL/pgvector、真实 Mem0 SDK、受控外部 HTTP。覆盖 TXT/PDF/DOCX/图片、当前范围、跨会话隔离、人物澄清、未批准不可召回、逐项确认/拒绝、重复提交、来源在嵌入途中丢失、失败与重试、提取及保存分别预算暂停/重启、否定和材料注入、伪造引文、停止后迟到结果。
- 浏览器 `pnpm --dir frontend test:attachment-memory`：默认端口 18112/18113，可通过 TEST_APP_PORT/TEST_MODEL_PORT 覆盖。实际上传→原文件出处→人物澄清→仅更新预览→确认一项/拒绝另一项→刷新→正式记忆来源，1 项通过，截图已人工检查。
- 前端构建通过；`test_attachment_memory.py`、`test_memory_candidates.py`、`test_memory.py`、`test_mem0_adapter.py`、`test_images.py` 共 37 项通过（37.43 秒）；完整 mypy 60 个源文件通过，TypeScript 通过。
- 已合入任务 16 集成 `fd20732`：历史查询在 `reserve_first` / `prepare` 最先保持只读，附件明确提取保持独立。合并后附件、历史与直接保存资料组合 20 项通过（19.25 秒），新增交叉用例核对旧提取指令和当前附带文件不会授权候选；下一条当前明确请求仍可提取。附件/历史/原消息候选浏览器共 4 项通过，完整 mypy 62 个源文件、TypeScript 与构建通过。
- 数据库/浏览器测试均通过共享串行锁，使用 15433 下本次测试创建的随机数据库，不读取真实配置或操作主服务。

真实云端附件提取质量、图片识别、嵌入服务兼容与实际费用均 **待验**。本轮未读取实际密钥、未发送付费请求；受控测试不代表真实服务已验收。
