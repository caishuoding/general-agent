# 任务 15：来源删除、影响预览与可恢复清理

## 产品与公开接口

会话页“删除当前会话”、每个附件旁“删除附件”先加载真实影响预览。全部关联事实默认勾选删除；只有 complete、memory_id 存在并经真实 Mem0 get 核对身份与正文的条目可以取消勾选以保留。未完成保存可删除，不能伪称有效保留。服务端核对关联集合、来源状态、版本及实际可保留状态；旧页面、重复保留 ID、非关联保留项返回409。确认后持久化唯一删除身份，同样决定的重复请求复用它，不能修改原保留选择。

- `GET /api/conversations/{cid}/deletion-preview` 与 `GET /api/conversations/{cid}/attachments/{aid}/deletion-preview` 返回名称、revision、关联记忆及 can_keep。
- `DELETE` 同一资源携带 `preview_revision`、`keep_memory_ids`；默认空表示全部关联项删除。200仅表示全部清理完成；503返回真实 failed 身份、阶段和安全错误。404表示来源不存在/已停用，409表示决定冲突。
- `GET /api/source-deletions` 返回未完成的无正文记录；`GET /api/source-deletions/{id}` 查原结果；`POST .../{id}/retry` 恢复原批准范围。前端独立恢复入口不依赖已经隐藏的会话。
- 删除无需模型调用或预算许可，实际已发生调用及费用继续保留。删除中的文件即使尚在磁盘也不能下载、发送、提取；删除会话立即从列表和读取中隐藏。

## 影响和正文边界

会话关联含原正式保存、已批准的候选派生动作、登记中的同一候选稳定动作、真实 memory_updates 更正来源。候选 target_id 仅表示拟变更目标，未批准/已拒绝时不算正式来源。附件仅按独立 attachment UUID → attachment_memory_candidates → 候选/动作追踪；不按名称或正文相等扩展到独立上传，也不把同消息里的独立直接保存事实当成附件派生。

`SourceDeletion` 先冻结来源并记录操作闭包，再复用 `MemoryDeletion.begin(connection, action_id)` 同事务登记默认删除墓碑，之后由唯一 `MemoryDeletion.delete(action_id)` 等待动作锁、完成真实 SDK 向量及历史清理；没有复制 SDK 删除逻辑。记忆恢复和旧来源抑制沿09规则。空原来源 ID 会被过滤，不写 NULL 抑制记录。

主动清理包括：原附件 UUID 文件、attachment_chunks、对应候选 fact/source_quote/target_fact/批准和整行 attachment_memory_candidates；所有关联 document_jobs/steps/final 缓存及消息进度；search_requests 查询/results/sources/purpose/旧批准；来源 citations、web_sources；关联 cloud_operations 的 result/response_status/approved/pause_id/reason，取消/失效并撤销 reserved 调用。`scope_hash` 只保存无正文幂等身份，保留；没有 scope 或 memory_action_operations 表。

会话删除另外清其消息、发现/候选操作关系、召回状态、搜索请求、历史查询与来源 hit；对其他会话历史引用主动移除 hit，保留原 result_count，使界面明确“来源不可用”。完整会话删除前断开 model_calls、cloud_operations、正式记忆和更正来源外键，保留费用及无正文墓碑。单附件删除保留已完成的 messages.content，活动/暂停的受影响回答清部分正文并停止；其他独立来源和既有聊天不整体清空。重试不把后来新开的无关活动回答加入原批准范围。

## 状态、并发和重启合同（供18）

迁移 `016_source_deletion.sql` 新增 conversations.deleting、attachments 的 deleting 状态、memory_actions.source_deleted、memory_updates.source_deleted，允许原 memory_actions 的 message_id/conversation_id 为空。source_deletions 只保存来源及关联 UUID 数组、revision、状态/阶段、错误，不保存预览事实、文件名或派生正文。

删除状态 running / failed / complete；阶段 invalidate → memories → files → relations → complete。首次接受事务锁原会话、附件和关联动作并校验预览；原来源 ID 的事务 advisory lock 保护重复受理，删除记录 ID 的 session advisory lock 保护恢复。只有默认记忆删除成功、指定文件不存在、关联 SQL 清理提交后才 complete。应用重启把 running 变成 failed，保持来源停用，用户重试同一身份。unlink 的 missing_ok 和09稳定墓碑保证部分完成可恢复。

`MemoryService.guard_source(connection, conversation_id, message_id, operation)` 在同一事务按来源会话→操作锁核验 still-valid 来源；直接/候选正式保存注册与发布、聊天更正发布和候选提交共用。管理页更正无原消息要求。来源文件守卫仍沿14入口、嵌入返回后和正式发布前执行。默认删除先墓碑后等待相同 action advisory lock，迟到 SDK 写入随后被唯一删除边界清理。解析提交先锁 attachment 并要求 processing，再插 chunks/更新 ready；失败也只改 processing，不能把 deleting 改回 failed。人物澄清锁来源会话；附件/搜索引用发布锁操作或会话，失效后不能回写。全文来源登记核对操作失效，删除的检查点不会重建；历史检索只锁和读取未删除中的来源会话。

**18 有效记忆入口**：用户保留项仍为 `memory_actions.status='complete'`、有效 memory_id、当前 version，`source_deleted=TRUE`。整会话物理删除时原 message_id/conversation_id 置NULL；单附件删除时这两个原消息字段可仍在，但附件来源行已清。列表采用 LEFT JOIN，显示来源已删除而不提供失效链接。保留项不经过 MemoryDeletion，也不再要求14原文件守卫；继续可召回、管理更正、忘记，并应参与18有效数据重建。source_deletions.status=complete 表示原批准的默认清理/保留标记/文件/关系阶段已全部完成；18自行沿动作身份、版本和锁核验最新有效状态，不能把来源已删等同于事实已删。

## 验证证据

按已批准公开 HTTP seam 逐项红绿，真实 PostgreSQL/pgvector15433、真实 Mem0 1.0.11；仅外部模型 HTTP 受控。必要物理清理契约检查数据库，SDK历史失败由真实数据库触发器产生，文件删除失败由真实文件系统目录替换产生。文件读取边界延迟用于重现解析晚到，未替换应用内部模块。

- `tests/test_source_deletion.py` 9项：两类删除、默认/保留/旧预览、同内容独立上传、重启后保留召回与再删除、SDK历史清理失败后恢复、全文/搜索缓存和批准清理、跨会话更正/历史、在途批准嵌入、unlink失败时晚到解析、未批准/拒绝目标隔离、全文晚到和旧重试禁止回退。
- 与09、14组合：30项一次通过，31.69秒。严格 `mypy --no-incremental` 68文件通过；前端类型及构建通过。
- 专项浏览器：真实上传、两项人物确认并保存、默认勾选删除、保留一项、来源标记、刷新、整会话删除后保留记忆继续管理，1项通过。默认 config testIgnore 与 test:e2e 均接入，专属默认端口18116/18117。
- 全文、搜索、历史、记忆更正与候选共享路径：46项一次通过，51.56秒；最后收紧搜索登记守卫后再跑11项搜索回归全部通过（11.74秒）。

真实云端模型、嵌入服务兼容性、搜索服务和实际账单仍待验；本票没有读取真实凭据、调用付费服务、改主服务或主数据库。按用户明确选择，本轮完成实现和受控验证，不把替身通过当成真实服务验收。
