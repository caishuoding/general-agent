# Word 内嵌图片描述、切片与原图召回

Status: ready-for-agent

Implement the Word image path described in `../spec.md` first.

- Extract decodable embedded images from DOCX and retain their anchor position.
- Generate a plain-text description with the configured `deepseek-flash` model and use it directly as chunk content.
- Store the image asset and description with a stable `image_id`.
- Return the image binding from document search and page reading.
- Inject the original image once in the next model request after tool recall.
- Preserve text-only behavior when image description fails.

## Comments

2026-10-22：Word 与 PDF 图片提取、结构化描述、稳定 `image_id`、搜索／读取绑定、
召回后原图注入、哈希缓存、单图失败隔离和生命周期清理已实现。验证见
[文档图片验证记录](../../../docs/verification-document-images.md)。

2026-10-23：模型改为直接输出纯文字描述，删除 JSON 字段协议；描述原样切片，继续绑定同一原图。
