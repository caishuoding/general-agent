# 最终审查修复验证

基线：`2592284`，对应 Standards / Spec 审查范围 `df47109...2592284`。本轮在独立 `codex/final-review-fixes` 树实施，不修改主服务或真实配置。

## 修复范围

- Spec P2（故事 56）：`AttachmentService.question_scope` 先确定本条选择或具名的已发送文件，再在该范围检索最多 12 块。没有重新选定或具名时，保留当前会话已发送资料的相关检索；具名失效文件不回退读取其他文件。图片沿原来的能力验证与实际读取规则。
- Spec P2（故事 67）：普通公开搜索中的“资料、图片、文件”等词不再强制附件。只有实际选择/具名来源，或明确引用当前资料时才准备附件并进入外发确认。未知 `.pdf` 后缀、公开 URL 不构成私人来源授权。“人类记忆研究、记忆宫殿、长期记忆机制、我的记忆力”等主题不会召回私人记忆；明确个人记忆引用和既有个性化语句仍准备真实来源并要求批准。
- Standards 硬规范：8 个故障注入 SQL trigger 函数定义前补充独立中文用途注释，覆盖 `reject_repeated_update`、`reject_purge`、`reject_add_history`、`reject_rebuild_progress`、`reject_space_publish`、`reject_update_history`、`reject_source_purge`、`reject_recall_fixture_history`。
- Standards 设计判断：共享历史→联网→普通附件/记忆/全文材料编排收进 `ChatExecution.prepare_response`，返回材料、可信来源上下文、全文检查点、本地结果及仍可用预留。同步与流式入口继续负责各自传输、状态持久化和停止语义，不新增执行引擎。
- Standards 设计判断：`MemoryService.maintenance` 提供维护租约，`retainable` 核验活动空间中的完整事实。`SourceDeletion` 显式接收正式记忆服务，停止穿透删除服务访问嵌入配置与 SDK。先租约、后来源行锁的顺序及唯一 `MemoryDeletion` 删除边界保持。
- 额外真实竞争：`App.refreshSources` 在发起刷新时捕获会话 ID 和选择代次，详情返回时重新核对，旧响应不再覆盖新选会话，也不能在 A→B→A 后复活旧详情。

## 红→绿证据

已批准测试 seam 是公开应用 HTTP、真实 PostgreSQL / Mem0、受控外部模型和搜索 HTTP，少量真实浏览器。未模拟内部服务。

1. `test_question_uses_current_or_named_file_even_when_old_file_has_many_chunks`：旧文件 `OLD_CODE=RED` 重复 2000 行，最新文件 `NEW_CODE=GREEN`。修改前当前选择和具名追问两项都回答 RED；修改后同步选择、异步具名追问都回答 GREEN，可信出处只指向最新文件。
2. `test_generic_web_topics_do_not_require_or_send_personal_attachments`：无文件时“北京旅游资料”等查询返回 502，有私人文件时误进 pending；修改后有无附件都直接完成公开搜索，精确查询等于用户主题、没有来源规划。补测 `.pdf` 和公开 PDF URL 后先复现 502，再改为仅非空当前/具名范围触发附件读取；“Python参考资料、参考资料的格式”也先红后绿。
3. `test_public_memory_topics_do_not_read_personal_memories`：无正式记忆时“人类记忆研究”返回 502，有正式记忆时误进 pending。补充“使用记忆宫殿…”及“我的记忆力…”也先复现；修改后这些公开主题均不增加嵌入请求、不生成个人资料规划、不附带私人来源。
4. 浏览器 `删除附件后的迟到详情不会覆盖新选会话`：删除 A 的附件后，取得后端真实 A 详情响应并暂缓返回；切到 B、确认 B 正文可见，再放行 A。修改前 B 被旧 A 覆盖导致断言失败；双重身份守卫后通过。测试只控制真实 HTTP 返回时机，不伪造会话响应。

## 最终针对性检查

以下 74 个不同 API 用例已通过；搜索规则最后收窄后重新验证最终审查与搜索的 17 项，未重复执行无变化的全部套件。

| 范围 | 结果 |
| --- | --- |
| `tests/test_final_review.py tests/test_search.py tests/test_attachments.py tests/test_images.py` | 32 passed（41.50s） |
| `tests/test_final_review.py tests/test_search.py`，最终搜索规则 | 17 passed（23.35s） |
| `tests/test_summary.py tests/test_history.py tests/test_streaming.py tests/test_memory_recall.py` 加预算阈值/单次批准、在途停止重试、取消许可三项 | 30 passed（30.66s） |
| `tests/test_source_deletion.py` 加重建中保留来源已删事实、在途来源删除、预算重启后清影子副本三项 | 12 passed（15.16s） |
| `playwright.source-deletion.config.ts` | 2 passed（8.5s） |
| `playwright.search.config.ts` | 1 passed（5.2s） |
| 默认配置下 `summary.spec.ts` | 1 passed（6.7s） |

严格 `mypy --no-incremental`：74 个源文件通过。`pnpm --dir frontend build`：TypeScript 检查与 Vite 生产构建通过。对全部改动 Python 文件扫描的 194 个函数均有中文 docstring；新增/修改前端回调与 8 个 SQL 定义逐项检查。`git diff --check` 通过。

所有数据库与浏览器命令均经 `/private/tmp/general-agent-implementation/serialized-test.py` 取得共享串行锁，连接 `127.0.0.1:15433`，每项测试自行创建并清理随机数据库。浏览器仅用 `18122/18123`，搜索替身用 `18127`。

## 共享接口与集成

`question_scope` 的 `None` 表示当前会话默认检索范围，空列表表示具名来源不可用，非空列表表示当前选择或具名的授权文件。普通问答遇空列表不回退其他文件；搜索只凭非空范围或明确资料引用启用私人资料规划，不能把未知文件扩展名本身当授权。

`prepare_response` 保持旧聊天优先于联网，联网优先于普通附件/记忆/全文的路由。返回的 `reserved` 仅是仍未开始的聊天预留；全文最终预留继续由全文检查点核验。历史与全文只附带当前问题，其余路径保留原会话上下文；传输入口负责最终模型调用、检查点收尾、可信来源解析及持久化。

`SourceDeletion.preview/accept` 先进入 `MemoryService.maintenance`，然后取得业务来源行锁；`retainable` 只核验 `complete` 且 SDK 中仍存在并与批准正文一致的事实，不因来源已删除标记排除有效事实。没有改变删除在所有登记空间清理向量和恢复的职责。

集成基线核对为 `2592284`；提交前合并最新 `codex/general-agent-integration` 并确认其为祖先。浏览器专项已在既有 `test:e2e` 链路和默认配置排除名单中，不新增漏跑的测试配置。

## 限制

本轮未连接用户真实云模型、嵌入或 Tavily，未读取 `.local`。真实服务兼容与效果仍按用户决定列为待验。自然语言意图沿项目明确短语规则识别；没有引入任意规划代理或额外自动外发权限。完整 pytest / 全浏览器由最终集成阶段统一执行，本记录只声明实际完成的针对性验证。


## 最终整合：浏览器保存提示选择器

父级在完整集成树运行的 Python 全套结果为 **179 passed（203.46s）**。全浏览器首次执行的默认组为 **7 passed / 1 failed**，失败位于 `frontend/e2e/chat.spec.ts:17`。已检查该次 `error-context.md`：聊天模型配置已保存成功，但全局 `getByRole("status")` 同时匹配空的来源清理状态区与真实保存提示，触发 strict mode violation；这是测试定位歧义。

在合入 `21a25a7` 后，仅将该断言改为精确定位用户可见的“配置已保存。修改后的连接请重新检查能力。”并检查可见，保留后续发送、刷新和会话切换验证。同步更新测试回调的中文用途注释。扫描全部 `frontend/e2e`，未发现其它全局 status 角色选择器。未修改产品，也未重新运行没有变化的 Python 套件。

通过共享串行锁、隔离随机 PostgreSQL 数据库和 `18122/18123` 受控端口执行 `playwright test chat.spec.ts -g '配置模型后可以对话并在刷新后继续'`：**1 passed（3.1s）**。`git diff --check` 通过；父级继续完整浏览器验收。
