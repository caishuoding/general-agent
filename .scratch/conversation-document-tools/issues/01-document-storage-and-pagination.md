Status: ready-for-agent

Completion: complete（2026-10-20）

# 原文存储、切片索引与固定分页

保存页／段落原文及切片映射，沿用会话归属；Word 自动转换固定分页副本。默认分片 1,500 字符、返回五片，可配置。旧附件从原文件重建，不延长 TTL。缺失分页运行时明确失败。

## Comments

- 2026-10-20：已完成实现和受控验证，详情见 [文档工具验证记录](../../../docs/verification-document-tools.md)。真实外部模型质量及部署镜像构建不属于本次受控验收结果。
