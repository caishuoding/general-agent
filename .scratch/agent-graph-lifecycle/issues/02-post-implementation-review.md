Status: ready-for-human

# 图重构代码与 State 字段复核

## TODO

- [ ] 还未 review 图重构代码。
- [ ] 还没有仔细看 State 字段有什么用、具体怎么做的。
- [ ] 逐个核对 State 字段的写入节点、恢复必要性、重置时机和跨轮保留策略。
- [ ] 复核 `settled`、`watching_inbox`、`inbox_deliveries`、`tool_failures`、`document_calls` 是否仍必要。
- [ ] 复核子智能体工具、自动收信和结束前检查是否仍符合既有权限、预算、幂等与恢复语义。
- [ ] 确认旧检查点升级、重试基线和会话稳定 thread 的切换策略。

## Comments

这是实现后的强制 review 项；代码通过测试不代表这些架构问题已经确认。
