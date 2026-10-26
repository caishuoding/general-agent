# OCR 结果接入附件与知识库

Status: ready-for-agent

Implementation: complete（2026-10-26）。公开入口回归、类型检查与双轴审查复核通过，完整结果见[验证记录](../../../docs/verification-pdf-scan-ocr.md)。

依赖[01](01-ocr-parsing.md)，按[规格](../spec.md)接入两条后台解析路径；整份解析与图片描述结束后，统一复用现有切片、表格、索引和引用算法。知识库提供 OCR 阶段进度；单页失败显示明确缺口，删除/撤销沿用发布前核验。规则升级保留 TTL、权限和人工清空状态。通过公开上传、检索、按页读取及引用验收。
