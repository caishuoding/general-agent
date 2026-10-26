# 扫描页版面 OCR 与中间解析结构

Status: ready-for-agent

Implementation: complete（2026-10-26）。公开入口回归、类型检查与双轴审查复核通过，完整结果见[验证记录](../../../docs/verification-pdf-scan-ocr.md)。

按[规格](../spec.md)提供 PP-StructureV3 独立服务及有界客户端。识别扫描页，逐页受限渲染，转换正文/标题/表格，裁出插图并绑定图注；保留阅读顺序、章节、原页坐标和识别缺口。中间结果只在内存中传递，复用 DocumentUnit 和 ParsedImage。解析公开入口按红绿循环覆盖结构、失败、资源和坐标。
