# Word／PDF 内嵌图片视觉描述验证

2026-10-22。

## 实现范围

- Word 从正文图片关系和媒体部件提取常见栅格图片，保留锚点段落与所在章节。
- PDF 逐页提取内嵌栅格图片；没有文字层的扫描页可继续通过图片描述形成切片。
- 图片统一解码、限制 4,000 万像素，并转换为 PNG 保存在
  `APP_DATA_DIR/attachments/<UUID>.images/`。
- 当前管理端模型配置为 `deepseek-flash`。视觉模型返回
  `ocr_text`、`caption`、`description` 和 `uncertainties` 四个结构化字段。
- 描述切片绑定全局唯一 `image_id`，并与普通文字共用 BM25、向量召回和重排。
- `search_documents` 和 `read_document` 返回 `image_id`；工具命中后，下一轮模型请求
  同时收到切片文字和对应原图。同一回答每张图片只发送一次，单批最多五张。
- 单份 Word／PDF 最多处理 50 张图片；同一文档内哈希相同的图片只调用一次视觉模型，
  但保留每个出现位置。
- 单张图片描述失败只标记该图片。只有既无正文、也没有任何成功图片描述时才判定文档失败。
- 附件到期或删除时清理派生图片目录、远端图片身份、`document_images`、描述切片和向量。

## 数据与升级

迁移 048 新增 `document_images`，并给 `document_chunks` 增加可空的
`image_id` 外键。Word 解析版本升级为 `word_native_markdown_v3`，PDF 升级为
`pdf_tables_v2`。启动恢复在解析版本、切片预算或视觉模型描述版本变化时重建已有含图附件，
不延长原 30 天有效期。

第一版不处理 SmartArt、EMF、WMF、复杂矢量图表和 Markdown 外部图片引用。
视觉服务失败、格式不受支持或资源超限均有明确状态，不把图片标记为已经理解。

## 验证

- 文档图片专项 5 项通过，覆盖 Word 描述检索与按页读取、召回后原图注入、
  无文字 PDF、单图失败隔离、重复图片描述缓存与到期清理。
- 完整后端回归 214 项通过（173.17 秒）；仅保留既存 pypdf
  `ZLIB_MAX_OUTPUT_LENGTH` 弃用提示。
- mypy 严格检查 121 个源文件通过。
- 变更 Python 文件中的函数和回调均有中文用途说明；`git diff --check` 通过。
- 测试使用受控 Files API 与聊天模型响应，不读取或上传私人文件。真实
  `deepseek-flash` 服务的视觉兼容性、延迟与描述质量仍需用用户授权样本验收。
