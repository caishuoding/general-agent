Status: ready-for-agent

Completion: complete（2026-10-21）

# 实现附件混合召回与智谱重排

依据 [规格](../spec.md) 与 [ADR 0013](../../../docs/adr/0013-attachment-hybrid-retrieval.md)，先更新文档，再实现 BM25、embedding 缓存、RRF、智谱重排及降级。完成类型检查、针对性测试、全量回归和双轴审查，提交本次改动及用户明确批准的附件递归切片前置改动。

## Comments

- 2026-10-21：已先完成文档，再实现；用户确认两个公共测试边界。全量 Python 测试 180 项通过，mypy 110 文件通过，Standards／Spec 均无未解决发现。真实 embedding 与智谱 rerank 用合成文字验证通过。详见 [验证记录](../../../docs/verification-attachment-hybrid-retrieval.md)。
- 用户已明确批准将工作区原有附件递归切片及迁移 042 作为依赖一并提交；其余既有会话状态改动保持原状。未重启正在运行的后端。
