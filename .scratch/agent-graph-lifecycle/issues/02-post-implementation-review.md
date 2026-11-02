Status: ready-for-human

# 唯一 Agent 图与 State 复核

- [x] 核对主从共用五节点图、AgentState 与 AgentContext。
- [x] 逐字段核对写入位置、恢复必要性、计数重置及跨轮保留策略；说明见 docs/agent-state.md。
- [x] 删除独立回答、工具使用标志、旧图 State、SkillSession、兼容入口和无调用包装。
- [x] 核对 Skill 工具静态注册、工作区直接使用、已加载 Skill 文件访问与路径越界拒绝。
- [x] 检查重试、自动信箱、后台命令、调用预算、幂等、来源撤销和继续执行语义。
- [x] 验证旧检查点一次性清理与业务历史保留，不在正常启动中维护旧结构分支。
- [ ] 维护者最终阅读五节点职责与 State 字段说明。

## Comments

2026-11-02：按用户确认的“从一张图开始设计”完成全面重构与自动检查；最终验证记录见 docs/verification-single-agent-state.md。此处保留维护者审阅项，不以自动测试代替人工架构判断。
