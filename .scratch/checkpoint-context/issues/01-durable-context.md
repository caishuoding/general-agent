Status: ready-for-agent

# 实现持久化执行上下文与自动压缩

规格：[spec.md](../spec.md)。完成外层及工具图 checkpoint、压缩、显式续接与前端入口，验证后提交当前分支。

## Comments

已开始实现，审查基准固定为 21394010。

实现提交 aa7b839d；审查修复提交 04993de8。Standards 与 Spec 复审均无剩余问题，核心集中复验 29 passed，最终串行完整回归运行中。正式服务数据库来源待确认，旧文件未清理。

实现与复审完成。最终核心 29 项、Skill/停止 10 项、工具图 12 项通过；前端构建及 3 项卡片回归通过。完整长运行仍受 PostgreSQL 意外断连影响，不报告全量通过；详情见 docs/verification-checkpoint-context.md。正式服务数据库地址仍待用户确认，未清理旧文件、未启动正式实例。

2026-11-01：用户授权选择项目默认库并清空全部旧数据，已完成重置及正式启动，保留连接配置。前端重新构建、后端/主页/管理端检查 200，Mem0 及 Skill 出口代理已运行；真实 Docker PPT 导出下载预览闭环 1 passed。正式启动阻断已解除。

2026-11-01 二次审计：删除旧首次预留链、独立 MemoryTools.prepare 工具图、context_messages、handled 与旧服务模型依赖；迁移 063 删除 memory_tool_runs，迁移 064 删除已无写入方的 Skill stage/route。工具额度集中到 LIMITS，断线续写重新压缩且只计算可用工具，Skill 图递归上限与动态 128 轮对齐。最终集中回归 53 passed，全项目 mypy 182 files、前端构建、真实 Docker PPT 1 passed。完整长套件 319 passed 后被本机 PostgreSQL 连接中断打断；重启数据库后受影响搜索 18 passed、记忆/来源删除 8 passed。


复审修复完成：finalize-only 恢复幂等关闭根 Agent；文档/子任务预算使用活跃工具；长期记忆删除清理派生检查点。工具策略、消息协议和 Skill 状态查询已移动到对应边界。复审补丁 35 passed，mypy 183 files；双轴剩余阻断 0。
