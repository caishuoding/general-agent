# 对话上传文件如何进入模型上下文

调研日期：2026-10-17

## 结论

不能把“在对话框上传文件”统一理解成“文件的全部内容被拼成一条普通用户文本，永久放进上下文”。OpenAI 官方公开资料显示，至少存在三种不同路径：

1. **直接文件输入**：文件是用户消息里的结构化 `input_file` / `file` 内容项，不是普通的 `input_text`。服务会按文件类型预处理，再把结果送给模型。
2. **检索路径**：ChatGPT 的文档功能和 API File Search 会对文件做文本提取/分块/检索；回答时使用与问题相关的内容。官方没有公开 ChatGPT 普通会话附件每一轮的完整编排细节，因此不能断言它始终全文注入，也不能断言它始终只做向量检索。
3. **工具读取路径**：Codex/ChatGPT Work 可以在执行环境中读取文件、运行代码和分析数据。这时文件可以作为工具可访问的资料存在，智能体按任务读取，而非必然预先塞满模型上下文。

最稳妥的产品表述是：**上传文件会成为当前任务可用的附件；系统根据产品、文件类型、大小和任务选择提取、视觉解析、结构化处理、检索或工具读取。模型实际看到的是处理后的内容或相关片段，不保证等同于原文件的全部字节，也不保证全部内容始终驻留在每轮上下文中。**

## ChatGPT 产品

### 官方明确说明

- ChatGPT 支持把文档、表格、演示、图片和音频附加到会话，用于总结、比较、查找信息、提取内容等。[Uploading files and audio to ChatGPT](https://help.openai.com/en/articles/8555545-uploading-files-and-audio-to-chatgpt)
- 对文档中的图片，官方明确区分两种处理：ChatGPT Enterprise 的 PDF 支持 visual retrieval；其他计划和文档类型采用 **text-based retrieval**，即从文件中提取数字文本，不处理内嵌图片。[Uploading files and audio to ChatGPT](https://help.openai.com/en/articles/8555545-uploading-files-and-audio-to-chatgpt#h_f667c0199d)
- Enterprise PDF 在对话中上传时可以同时读取文本和内嵌图片/图表；但作为 GPT Knowledge 或 Project Files 保存的 PDF 仍使用 text-only retrieval。在已发布 GPT 的交互或 Project conversation 中临时上传的 PDF 才使用 visual retrieval。[Visual Retrieval with PDFs FAQ](https://help.openai.com/en/articles/10416312-visual-retrieval-with-pdfs-faq)
- 当前主要限制：文档和演示每文件最大 512 MB；文本和文档每文件最多 200 万 token（表格不适用此 token 限制）；CSV/表格约 50 MB；图片每张 20 MB；音频 512 MB。[Uploading files and audio to ChatGPT](https://help.openai.com/en/articles/8555545-uploading-files-and-audio-to-chatgpt)
- 页面列出的常用格式包括 DOCX、PDF、TXT、XLSX、XLS、CSV、TSV、PPTX，以及多种音频格式；`.gdoc` 需先导出为 PDF 或 DOCX。[Uploading files and audio to ChatGPT](https://help.openai.com/en/articles/8555545-uploading-files-and-audio-to-chatgpt)

### 官方没有公开的部分

- 没有公开保证“普通聊天附件的全文会作为一条普通 user message 一次性注入”。
- 没有公开普通聊天附件在每次追问时究竟会注入多少片段、使用何种排序算法或是否缓存全文。
- “每文件 200 万 token”是上传/处理上限，不等于模型在单次请求中一定获得这 200 万 token，也不等于模型上下文窗口至少为 200 万 token。

因此，若要复刻 ChatGPT，不能仅凭产品行为选择“把全文拼进 prompt”；需要自己确定全文输入、检索增强或工具读取策略。

## Codex 与 ChatGPT Work

OpenAI 的产品文档说明：

- Chat、ChatGPT Work 和 Codex 是不同工作方式；Codex 面向开发工具和技术细节。ChatGPT Work 可以读取文件、分析数据、运行代码和 shell 命令，并检查执行环境可访问的文件。[Use ChatGPT](https://learn.chatgpt.com/docs/use-chatgpt)
- 可以附加文档、演示、表格、PDF、图片和数据导出，用于总结、比较、找规律、清洗重组或作为新文件的素材。[Use ChatGPT - Attach files](https://learn.chatgpt.com/docs/use-chatgpt#attach-files)
- 云端工作可以使用上传文件；本地工作可以使用计算机上的文件、应用或浏览器。[Use ChatGPT - Choose cloud or local work](https://learn.chatgpt.com/docs/use-chatgpt#choose-how-you-want-to-work)

这些文档证明附件可成为智能体的任务上下文或执行环境资料，但**没有公开 Codex 桌面端附件的内部序列化格式，也没有承诺上传后把全文作为普通用户文本发送给模型**。Codex 还具备文件系统和工具读取能力，所以更合理的抽象是“附件/文件是可读取资源，智能体按需读取”，而不是“附件等同于一大段聊天文本”。

## OpenAI API：实现机制最明确的参照

Responses API 把文件表示为用户输入中的独立 `input_file` 内容项，与 `input_text` 并列；Chat Completions 则把 PDF 表示为独立的 `file` content part。因此它们属于用户角色输入的一部分，但不是普通文本字符串。[File inputs](https://developers.openai.com/api/docs/guides/file-inputs)

官方公布的处理规则如下：

| 文件类型 | Responses API 的处理方式 |
| --- | --- |
| PDF | 视觉模型会提取文本和每页图像，并把两者送给模型；两部分都计入上下文/token 使用量。 |
| DOCX、PPTX、TXT、代码等非 PDF 文档 | 只提取文本；内嵌图片和图表不会进入模型上下文。 |
| XLSX、CSV、TSV 等表格 | 不传整张表；每个 sheet 最多解析前 1,000 行，并加入模型生成的摘要和表头元数据。 |

直接文件输入的限制是每个文件小于 50 MB，单请求所有文件合计不超过 50 MB。官方建议大文件改用 File Search，而不是直接作为 `input_file`；File Search 面向“对分块文件进行检索”，避免把整个大文件塞进单一上下文窗口。[File inputs - Usage considerations](https://developers.openai.com/api/docs/guides/file-inputs#usage-considerations)；[File Search](https://developers.openai.com/api/docs/guides/tools-file-search)

这组 API 规则不能直接证明 ChatGPT 或 Codex 的内部实现完全相同，但它明确展示了 OpenAI 的公开接口并不是“无条件将文件全文变成普通文本”。

## 对本项目的建议

本项目术语把附件定义为“用户在某个会话中上传、供对话智能体理解内容和回答问题的文件”，且附件只归属于上传它的会话。这个定义与上述调研兼容，不需要把附件等同于聊天消息正文。

建议将实现和产品说明拆成三层：

1. **原始附件层**：保存原文件、类型、大小、会话归属和删除状态。
2. **派生内容层**：保存文本提取、页码/段落、表格结构、图片/OCR 等可追踪结果。
3. **回答上下文层**：小文件或强依赖全文的任务可直接输入；大文件优先分块检索；复杂表格/文档由工具按需读取。引用应指回附件及具体位置。

不要在界面或规格中承诺“上传后完整加载到上下文”。更准确的用户文案是：“附件会在当前会话中供智能体按需读取和检索；受格式、大小和模型能力影响，部分视觉元素或超长内容可能不会一次性送入模型。”

## 证据强度与边界

- **高置信度**：API 的逐类型处理方式、ChatGPT 的格式/大小限制、Enterprise PDF visual retrieval 与其他场景 text-based retrieval，均来自 OpenAI 官方文档。
- **中等置信度**：Codex/Work 把上传文件作为可读取任务资料，而非必然普通文本；官方能力说明支持这一抽象，但未公开内部请求编排。
- **未知**：当前 Codex 桌面版或 ChatGPT 普通会话在某一具体版本中，对某个附件每一轮实际注入了哪些 token。公开文档不足以回答，且可能随模型、计划、客户端与服务端版本变化。
