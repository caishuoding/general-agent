# 会话文档工具验证

验证日期：2026-10-20。规格见 [.scratch/conversation-document-tools/spec.md](../.scratch/conversation-document-tools/spec.md)，决策见 [ADR 0011](adr/0011-conversation-document-tools.md)。在已有未提交的 DeepSeek 图片和搜索改动基础上实施，保留并行新增的 LangSmith 追踪改动。

## 实现结果

- 迁移 041 为所有附件保存上传登记后 30 天的 expires_at，并增加结构原文、切片及 GIN 关键词索引；已有文档从原件重建，不续期。
- 每轮只提供 document_id、filename、pages／paragraphs 清单；正文无大小分支，不自动注入。图片沿用既有 file_id。
- search_documents 按配置片数返回完整切片，不计算返回 Token 预算；默认 1500 字符／五片，可通过环境调整。
- read_document 从结构原文按页／段落连续读取，完整序列化结果及图追加计数字段计入单次 10000 Token 上限，且受执行点剩余预算限制；结果含位置、complete 和 next_cursor。
- 两个文档工具分别每轮最多五次，同批调用、失败、续读、畸形参数均受限制；工具选择交给模型，没有任务路由和强制检索流程。
- PDF 和 Word 统一固定分页；Word 原件及固定 PDF 均保存。macOS 无界面 LibreOffice 通过独立配置显式加载系统中文字体，避免缺字导致 PDF 字符映射错误。中文正文与表格均已实测。
- 到期先禁止访问，再清原件、副本、原文、切片和远端图片；启动及每分钟重试失败清理，已完成聊天正文保留。
- 结构原文最多 20000 个换行段落单元，防止大量短行在索引前耗尽服务进程内存；PDF／DOCX 仍执行原解析资源限制，Word 转换最多 60 秒／80 MB。

## 受控验证

- 完整后端回归：`pytest -q --maxfail=5`，130 项通过（初次整体验证）。
- 文档、知识库、Token 及工具循环补充验证：26 项通过。
- 中文字体修正后的 PDF／Word 与知识库回归：18 项通过，包含中文正文和表格完整提取断言。
- document_id 统一后的文本附件、知识库和联网搜索来源回归：29 项通过。
- Python 严格类型检查：`mypy backend tests --no-incremental` 通过；函数用途说明检查通过，`git diff --check` 通过。
- 前端 `pnpm --dir frontend build` 通过；本环境需将内置 Node 可执行目录加入 PATH。
- 图片浏览器回归 `pnpm --dir frontend test:images`：1 项通过。

测试使用真实独立 PostgreSQL 测试库、程序生成 PDF／DOCX／PNG，以及 HTTP 边界模型替身。覆盖无正文自动注入、重启、长文末尾召回、会话／账号隔离、跨页和段内无损续读、JSON 转义后预算、真实模型用量约束、两工具独立限额、TTL 下载及工具拒绝、清理失败恢复、图片远端清理、既有聊天保留和旧附件升级。

## 配置与验证边界

- 官方分词运行库 tokenizers 已加入 pyproject.toml、uv.lock 和 requirements-server.txt；用本地可控 tokenizer.json 验证精确模型匹配、禁用截断及字符回退。具体模型官方文件须通过 MODEL_TOKENIZERS 映射配置，不能把测试夹具视为已验证真实 DeepSeek 分词资产。官方文件入口见 [DeepSeek Token 用量计算](https://api-docs.deepseek.com/zh-cn/quick_start/token_usage/)。
- 模型窗口和输出预留在管理端配置。文字消息无服务用量时使用保守估算；多模态消息缺少实际用量时不能凭 file_id 推断图片占用，原文读取返回预算不足。本次没有新增滑动窗口或摘要压缩机制。
- Dockerfile 增加 Writer 和 Noto CJK；本轮没有构建部署镜像或更新运行中的服务。
- 未将私人文件发给真实外部模型；真实模型的检索策略、回答质量和分页阅读质量仍需服务验收。

## 2026-10-21 递归重叠切片

- 迁移 042 增加结束原文单元，并显式删除 knowledge_version<2 的旧切片及关键词索引；原文、附件及新切片保留。检索返回起止页／段落及各自偏移，短段和跨页切片可准确定位。
- 自然段按空行识别，单换行及 CRLF 原样保留；按段落、句子、分句、空白、字符逐级选择切点，Markdown 标题和中文章节边界禁止跨片和重叠。
- 默认上限 1500 字符（包含重叠），overlap 目标 200 字符；优先整句，放不下新句时缩减重叠，超长句才使用分句或字符兜底。可通过 DOCUMENT_CHUNK_OVERLAP 配置，0 禁用。
- 启动恢复将 knowledge_version 0／1 的有效文字附件升级为 2，保留原到期时间；解析器不再额外生成固定 1500 字符正文片段。
- `pytest tests/attachments -q --tb=short`：54 项通过，包含 11 项递归切片测试、随机输入无遗漏／持续推进、两种旧版升级及跨段／跨页位置还原。相关 14 个源文件和测试文件的严格类型检查、函数说明检查及差异空白检查通过。
- PDF 仍依据提取文本中的空行划分段落，未增加版面分析、OCR 或嵌入模型；未重启运行中的服务。
