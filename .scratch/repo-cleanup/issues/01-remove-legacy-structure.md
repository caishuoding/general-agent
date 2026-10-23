# 01: Remove legacy database and code structure

Status: ready-for-agent

Completion: complete（2026-10-23）。

## 实现

- 添加迁移 051，删除旧搜索确认字段、回答版本字段和图片结构化字段。
- 添加迁移 052，删除旧 `langgraph` schema。
- 图注写入 `document_images.caption`，图片描述仍写入 `description`。
- 移除无调用方方法及对应回调。
- 清理无引用前端 CSS，并更新过时文档。

## Comments

2026-10-23：删除前将字段和旧 checkpoint 数据备份到 `/tmp/general-agent-cleanup-backup-20261023-131641`。完整 Python 回归 238 项、全部浏览器配置 20 项、MyPy 132 文件和前端构建通过；迁移 051/052 已应用到当前数据库。
