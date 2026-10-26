# 扫描 PDF 使用版面 OCR，正文与插图复用现有处理链路

2026-10-26。用户确认整页扫描 PDF 应先由 PaddleOCR PP-StructureV3 识别正文、标题、表格和插图区域；正文转为现有 DocumentUnit，插图裁剪后与图注一起交给既有视觉模型描述，整份中间结构完成后再统一切片。此决定替代 ADR 0011 的“扫描件不做 OCR”范围限制，继续遵守 ADR 0014/0015 的共用算法与独立存储合同。

OCR 模型放在独立的本地 HTTP 服务中常驻，应用只传页面图片并校验结构化结果，避免模型加载挤占现有文字解析工作进程的 20 秒/512 MiB 资源预算；代价是需部署额外运行时和模型缓存。扫描原页始终保留；识别坐标映射回原始物理页，文字、表格、图片描述各自形成现有类型切片，不另建 OCR 正文表，也不把整页扫描图当作插图描述。第一版中间结果在内存中传递，失败重试可重新 OCR；持久化缓存另行评估。

固定依赖与协议以[扫描 PDF 规格](../../.scratch/pdf-scan-ocr/spec.md)为准。依据：[PP-StructureV3 官方使用说明](https://github.com/PaddlePaddle/PaddleOCR/blob/v3.3.2/docs/version3.x/pipeline_usage/PP-StructureV3.en.md)。
