# 任务 10：会话文本附件与可信出处

日期：2026-10-15。基线为已集成 01–04 的 `10ef7ad`，独立分支 `codex/ticket-10-attachments`。所有测试使用随机数据库与受控外部 HTTP 响应；没有读取主 `.local`、访问用户数据库、调用真实模型或修改主 8000 服务。

## 已实现行为

- 在聊天框添加 TXT / Markdown（`.txt`、`.md`、`.markdown`），选择本条消息的文件后发送。界面展示上传中、处理中、可用及失败状态，文件失败会给出原因；发送不可用文件由后端拒绝。上传与本地解析本身不发云请求。
- 每次上传生成独立附件和原文件，不按内容摘要共享文件生命周期。附件只能在所属会话列出、下载、绑定和检索。原会话重新进入后可继续追问，新会话必须重新上传。已上传但尚未随任何消息发送的文件不会进入模型。
- 后端同时限制每个上传请求及每条消息最多五个文件；分批上传不能绕过消息限制，重复 ID 明确拒绝。单文件按实际字节执行 **20 MB = 20,000,000 字节**，恰好上限可用。头部、总请求和文件数量分别限额；不依赖前端或 Content-Length。
- `python-multipart==0.0.32` 增量解析；必须实际出现终止边界才成功。截断、坏头、超限与异常都关闭本次全部暂存句柄并删除暂存字节，不将不完整上传登记为附件。随机存储路径与原始显示文件名分开。
- 严格读取 UTF-8 / UTF-8 BOM；空文字、无法解码、二进制控制字符均持久记录失败，不以忽略字节假装成功。其他格式在当前票明确拒绝，后续 11/12 扩展格式。
- 文本按原始行号切成最多 1500 字符片段，超长行保留行内字符范围。PostgreSQL 在当前会话资料中按英文词与重叠中文双字词检索，仅返回最多十二段相关材料。该集合与 Mem0 完全独立，不要求嵌入配置。
- 当前用户消息与资料 JSON 分开。资料中的命令没有保存记忆、联网等动作授权。同步 `/messages` 和浏览器 `/runs` 都使用同一 `AttachmentService`，所有实际回答请求经过既有预算网关。
- 来源键使用 PostgreSQL 全局、持久的片段编号（`A{citation_id}`），跨轮换排序不会把相同标识映射到另一段。回答仅将本次实际提供且仍有效的来源呈现为可信引用；未知标记替换成“出处未核验”。资料回答无合法引用时显示“缺少可核验的资料引用”。引用展示原文件、真实位置和摘录，可下载原文件核对。合法出处不等于模型结论语义必然正确。
- 重试复用原用户消息及附件关系，保留旧失败尝试；停止后不再修改已见文字，已发出用量继续收尾。解析中实际后端进程被终止后，重启将遗留 processing 标为失败，保留已登记原文件，提示重新上传。

## 数据及 HTTP

`011_attachments.sql` 新增 `attachments`、`attachment_chunks`、`message_attachments`，消息新增 JSONB `citations`。业务状态、行号、独立检索正文、关系与引用都在 PostgreSQL。原文件在 `APP_DATA_DIR/attachments/<attachment UUID>`（默认 `.local/attachments/`）；暂存文件使用随机 `.upload` 后缀，成功登记后改为 UUID。目录及文件分别以 0700 / 0600 新建。原文件下载为 `application/octet-stream` / attachment，不执行资料中的 HTML。

| 接口 | 行为 |
| --- | --- |
| `POST /api/conversations/{id}/attachments` | multipart 的 `files` 字段，可重复至五个；201 返回已登记的 processing 附件，后台解析。 |
| `GET /api/conversations/{id}/attachments` | 当前会话附件与 ready / failed / processing、错误原因、大小。 |
| `GET /api/conversations/{id}/attachments/{attachment_id}/file` | 只下载所属会话原文件，跨会话 404；不暴露磁盘路径。 |
| 既有 `/messages`、`/runs` | body 增加可选 `attachment_ids`，最多五个且必须属于当前会话并已 ready；ID 列表属于操作范围指纹。 |
| 既有会话读取与 SSE | Message 增加 `attachment_ids` 与后端核验的 `citations`，刷新可恢复。 |

## 共同执行接缝与后续票合同

`AttachmentService` 是资料深模块，不创建第二套聊天执行器：

- `validate(conversation_id, attachment_ids)` 在任何云请求前校验；`bind(connection, conversation_id, user_id, attachment_ids)` 在保存用户消息事务内再校验并绑定。
- `prepare(conversation_id, user_id, content, attachment_ids=None) -> AttachmentContext(messages, citations)` 返回独立资料消息与本次引用白名单，不发云请求、不触发业务授权。`user_id` 限制到该消息之前已发送的文件；可选 `attachment_ids` 只给原同步实现尚未提交的当前消息使用，必须由上游先校验，不能从模型返回内容取得。
- `resolve(conversation_id, answer, material)` 核验并返回 `(cleaned_answer, citations)`；`save_citations(message_id, conversation_id, material)` 用于流式结尾，已停止消息仅更新引用，冻结正文。
- 当前同步与流式实现都已接入。并行 05 负责共同 `ChatExecution` seam；合并时在明确动作分支之后、真正回答调用之前准备资料，不把 material 传入用户意图识别，也不提前预留未到达的最终聊天调用。05 同步消息先提交后，可以直接用稳定 `user_id` 代替当前 `None + attachment_ids` 兼容路径。
- 11/12 应扩展同一登记与解析结果，保留片段编号及真实位置；13 的全文处理不能复用 top-12 冒充全文覆盖。当前仅做具体资料问题检索，全文总结和跨文件全面比较由 13 完成。
- 15 删除必须同时清理原文件、chunks、message_attachments、历史 Message.citations 及预算操作缓存正文；已绑定的操作身份可由消息与 cloud_operations / model_calls 关联定位。删除资料不得影响别的会话独立上传副本。当前票不提供绕过关联记忆确认的删除接口。
- 18 的嵌入空间重建不影响本票纯本地关键词检索；如后来改为向量，仍必须使用独立资料索引和统一调用网关。

## 验证记录

已确认 seam 为公开 HTTP 行为、真实 PostgreSQL 和外部 HTTP 替身，补少量真实浏览器。没有内部模块 mock、伪造数据库或真实付费请求。红灯覆盖：不存在上传接口、浏览器缺少上传入口、无合法引用缺少明确提示、跨轮 A1 重新指向不同原文、中文奇数偏移词组漏检文末、超过五个文件只显示泛化字段错误；逐项修复后验证通过。

- `TEST_DATABASE_URL=...:15433/postgres .venv/bin/python -m pytest -q`：完整 **50 项通过**，含 11 项附件接口 / 实际进程重启验收。
- `.venv/bin/python -m mypy backend tests`：**28 个文件通过**。
- `pnpm --dir frontend build`：TypeScript 与 Vite 通过。
- `TEST_APP_PORT=18090 TEST_MODEL_PORT=18091 TEST_DATABASE_URL=...:15433/postgres pnpm --dir frontend test:e2e`：聊天与附件 6 项、能力 1 项、预算 1 项，完整 **8 项通过**。当时独立能力 / 预算配置分别使用其默认 18084/85、18086/87，与并行 05 的 18088/89 无冲突。
- 收尾新增“失败附件提交保留问题草稿”的浏览器红灯，修复后再次 build 及单个附件浏览器流程通过；没有改动后端逻辑，无需重复已经通过的完整后端套件。
- 覆盖普通 / 流式、重启后追问及下载、未发送上传不进入上下文、跨会话读取 / 绑定拒绝、重复上传独立 ID、4/5/6 上传数及分批后消息数量限制、重复 ID、19,999,999 / 20,000,000 / 20,000,001 实际字节、截断上传与暂存清理、分片请求、UTF-8 BOM、空 / 无法解码 / 二进制文本、未支持格式、虚构出处、跨轮稳定来源、中文文末检索、预算批准及幂等、失败重试 / 停止、实际后端解析中 kill / restart。
- 浏览器截图已查看：`/private/tmp/general-agent-implementation/attachments-10.png`；状态、出处摘录与失败提示可见，无遮挡。
- AST 检查新增 Python 文件所有函数都有中文 docstring；手工检查前端函数与回调用途注释，`git diff --check` 通过。

## 真实服务验收

真实模型的资料理解、自然引用质量及供应商上下文限制仍 **待验**；受控 HTTP 证明应用的数据隔离、调用计量与协议行为，不证明真实模型回答质量。用户已明确本轮先完成实现、缺配置真实验收标待验。本票本地检索不依赖嵌入服务；未测试真实云端资料问答，不将任务 01 的一次文字连通性当成本票验收。
