# 01: 账号、会话、审计结构与升级清空

**What to build:** 增加账号、登录会话和管理员审计的数据结构；为会话、调用、搜索、记忆动作和云操作增加账号归属；在升级前生成可恢复备份，然后清空现有业务数据并预置初始管理员。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

**Completion:** complete（2026-10-19）。

**规格覆盖：** 登录与后台账号管理规格，用户故事 1、7、14、16、17；实现决策中的账号、初始管理员、数据归属与迁移。

- [x] 新增 `users`、`sessions`、`admin_audit_log` 表；账号字段覆盖用户名、密码哈希、角色、状态、强制改密和登录时间。
- [x] 为 `conversations`、`model_calls`、`search_requests`、`memory_actions`、`cloud_operations`、`memory_recalls` 增加可查询的账号归属。
- [x] 用户名大小写不敏感且全局唯一；角色和状态有数据库约束。
- [x] 初始管理员 `admin / 123456` 使用 Argon2id 哈希预置，并标记为必须改密。
- [x] 升级流程先备份 PostgreSQL 和附件，备份失败时停止清空。
- [x] 清空现有会话、消息、附件记录与文件、记忆、来源、搜索、用量和相关操作记录，保留模型、嵌入和搜索配置。
- [x] 迁移可重复执行且不会重复清空新账号数据。
- [x] 测试覆盖升级前数据存在、备份成功、业务数据清空、配置保留和初始管理员可用。
- [x] 所有新增或修改的函数都有中文用途说明注释。

## Comments

2026-10-19：已实现并通过完整自动化验收，见 [登录验收](../../../docs/verification-login.md)。
