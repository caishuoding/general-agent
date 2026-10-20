# DeepSeek 图片文件上传验证

日期：2026-10-20。用户原要求两个月 TTL，经核对官方上限后明确选择 30 天。

## 当前行为

- PNG/JPEG 在本机校验后，通过当前 LLM 地址与密钥 `POST /files`。multipart 包含 `purpose=user_data`、`expires_after[anchor]=created_at`、`expires_after[seconds]=2592000`。
- 返回的 `file_id`、服务端创建与过期时间、服务地址持久化到业务库。聊天使用扁平 `{"type":"file","file_id":"…"}` 内容块，重启及追问复用，原图仅上传一次。
- 过期后要求用户重新上传，不自动续期。旧版只有本机文件的图片首次使用时补上传；更换服务地址后拒绝引用旧服务文件身份。
- 删除附件或会话先撤回来源，再清理远端文件及本机原文件；失败保留删除身份。图片上传和删除使用独立的建议锁命名空间，迟到上传结果仍会被原删除流程清理；文档解析不等待这把锁。

## 自动化结果

`pytest tests/attachments tests/memory/test_source_deletion.py tests/app/test_conversation_graph.py tests/app/test_agent_graph.py tests/search/test_search.py -q`：46 项通过。

`pnpm --dir frontend test:images`：1 项浏览器流程通过。浏览器使用受控 Files API，检查真实 multipart 字段、上传后 file_id 视觉输入及会话隔离。

`mypy`：99 个源文件检查通过。`git diff --check` 通过。

覆盖上传失败、缺少或超长 TTL、过期拒绝、重启复用、会话隔离、旧图片补上传、更换服务、远端删除失败重试和上传/删除交错。

## 真实接口验证

使用当前 LLM 的 DeepSeek 连接上传程序生成的 16×16 红色 PNG；服务端返回 `expires_at-created_at=2592000`，随后立即通过文件删除接口成功清理。未读取或上传用户现有图片，未在文档及日志输出密钥。

本项真实验证覆盖 Files 上传、30 天 TTL 与删除。实际模型基于 file_id 的视觉回答质量未在本轮调用真实模型验收，协议和页面链路由受控测试验证。

官方协议及字段依据见 [Files API 研究笔记](research-deepseek-files.md)，决策见 [ADR 0009](adr/0009-conversation-attachment-full-context.md)。
