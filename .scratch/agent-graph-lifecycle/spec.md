Status: ready-for-human

# 唯一 Agent 图与精简 State

2026-11-02 的用户确认替代此前全部双图与兼容方案。以只有一张图为前提全面重构，不保留旧执行器、第二套 State 或兼容包装。

- 主从共用 AgentState、AgentContext、AgentRunner 和 build_agent_graph。
- 固定五节点：prepare_context、prepare_model、call_model、execute_tools、finalize。
- State 保存消息、Run 归属、轮次、loaded_skills、附件与知识库清单、待完成子任务、失败和文档次数、完成标志与错误。最终正文和工具调用由 messages 表达，不重复保存 answer 或 used_tools。
- loaded_skills 为独立顶层字段，含名称、版本、根目录与入口；不引入 SkillSession。
- Skill 工具与普通工具一样预先注册。读取 Skill 文件要求已加载；工作区工具直接校验账号、会话、Run 与允许访问的路径。
- 保留记忆、附件、图片、知识库、搜索、子任务、压缩、流式、停止、重试、继续执行和副作用幂等。
- 主回答使用会话稳定线程，子任务按 Run 使用独立线程。新回答清理旧工具轨迹；恢复从原待执行位置继续。
- 迁移一次性废弃旧检查点与旧未完成执行，业务历史保留；不为旧 State 编写转换层。
- 全面检查旧类、旧导入、未使用包装、冗余字段、测试和当前文档。新增和修改函数均有中文用途注释。

字段及用途见 [Agent State](../../docs/agent-state.md)，架构决定见 [ADR 0023](../../docs/adr/0023-unified-answer-graph.md)。

## Comments

实现后需完整回归、静态检查、迁移验证及服务重启检查；维护者人工复核仍记录在 issues/02-post-implementation-review.md。
