Status: ready-for-agent

Completion: complete（2026-10-21）

# 对话图 State 数据流重构

按 [ADR 0012](../../docs/adr/0012-state-based-conversation-flow.md) 重构外层对话图，使节点返回值直接表达执行结果。

## 验收

- prepare_context 返回模型消息、文档来源标识及调用预留，agent 从 State 读取并返回完整工具消息、回答和执行标志，finalize 返回核验后的回答和来源。
- runtime.context 不可变，只携带固定身份、模型配置和传输参数；节点不得把运行结果写入其中。
- 外层停用 PostgreSQL checkpointer，不新存正文副本；业务消息、停止、重试、幂等和历史检查点删除行为保留。
- 同步及流式成功、准备失败、模型失败、部分输出、收尾异常均保存正确终态；失败准备不调用模型。
- 追踪中的节点更新可观察，不包含模型密钥；普通回答、文档工具、联网及来源删除回归通过。
- 保留附件实际使用待测清单，不把本次受控回归当作真实文件问答质量验收。

## Comments

- 2026-10-21：用户授权按 LangGraph 常规 State 共享方式重构，先记录决策再实施。

- 2026-10-21：已完成重构及受控回归，详见 [验证记录](../../docs/verification-state-sharing.md)。
