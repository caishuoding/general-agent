# DeepSeek Files API 与图片输入核对

核对日期：2026-10-20。仅采用 DeepSeek 官方文档；未调用真实上传或推理接口。

## 上传与 TTL

图片上传使用 `POST https://api.deepseek.com/files`，认证为 `Authorization: Bearer <DeepSeek API Key>`，请求体为 `multipart/form-data`。必填字段 `file` 为二进制图片，`purpose` 为 `user_data`。格式按实际内容识别，支持 JPEG、PNG、GIF、WebP，单文件上限 64 MiB。[Upload File](https://api-docs.deepseek.com/api/create-file/)

可选过期设置必须同时传递两个 multipart 字段：

| 字段 | 值 |
| --- | --- |
| `expires_after[anchor]` | `created_at` |
| `expires_after[seconds]` | 3600–2592000 秒，即 1 小时至 30 天 |

省略两字段表示永久保存。上传成功返回 `id`、`object`、`bytes`、`created_at`、`filename`、`purpose`；设置过期时另有 Unix 秒级时间戳 `expires_at`。**官方接口不能直接设置两个月 TTL。**[Upload File](https://api-docs.deepseek.com/api/create-file/)

上传需在 10 分钟内完成。存储按 DeepSeek 账户计算，上限 25 GiB、10000 个文件。所有同账户 API Key 共享文件，OpenAI 与 Anthropic 两套兼容接口可引用同一文件；Anthropic 文件接口及引用另需 `anthropic-beta: files-api-2025-04-14`。[Files API](https://api-docs.deepseek.com/guides/files_api/)

## Chat Completions 图片内容块

`deepseek-flash` 支持图片输入，旧名 `deepseek-v4-flash-vision-exp` 已下线，其请求由最新 Flash 模型承接。Chat Completions 的图片内容只允许放在 `user` 消息；`system`、`assistant` 中图片返回 400。[Vision](https://api-docs.deepseek.com/guides/vision/)

请求中使用扁平的 `file_id` 内容块，**没有嵌套的 `file` 对象**：

```json
{
  "model": "deepseek-flash",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "描述这张图片"},
        {"type": "file", "file_id": "file-api-xxxxxxxxxxxxxxxx"}
      ]
    }
  ]
}
```

`file_id` 与内联字段 `file_data` 互斥；`filename` 仅可与 `file_data` 一起提供。不要将其他供应商 SDK 的嵌套文件格式直接用于 DeepSeek。[Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)

文件 ID 引用的单图可达 64 MiB；单请求最多 600 张图，包含文件 ID 图片时图片总大小最高 200 MiB。单边最长 8192 像素，请求中有至少 15 张图时上限降为 4096 像素。[Vision](https://api-docs.deepseek.com/guides/vision/)

## 查询元信息、删除与过期边界

- `GET https://api.deepseek.com/files/{file_id}` 返回文件元信息，包括可选 `expires_at`；该端点不是原文件下载接口。[Retrieve File](https://api-docs.deepseek.com/api/retrieve-file/)
- `DELETE https://api.deepseek.com/files/{file_id}` 返回 `id`、`object: "file"`、`deleted: true`。[Delete File](https://api-docs.deepseek.com/api/delete-file/)
- 已核对的 Files 指南及 API 参考未列出延长 TTL 的更新接口，也未承诺过期文件引用的具体 HTTP 状态或错误码。不能将某个 400/404 直接等同于过期。[Files API](https://api-docs.deepseek.com/guides/files_api/)
- 同账户换 Key 可共享文件是文档明确保证；跨账户 Key 不在该保证内。应用不应把文件视为全局公共资源。[Files API](https://api-docs.deepseek.com/guides/files_api/)

## 错误处理

官方通用错误页列出：400 请求格式错误、401 密钥认证失败、402 余额不足、422 参数错误、429 请求速率或并发限制、500 服务端错误、503 服务繁忙。500/503 建议等待后重试；凭据和参数类错误需先修正输入。Files 参考页仅列成功 schema，没有定义文件过期或不存在的专用错误协议；真实失败须保留可诊断信息，不应编造成功文件 ID。[Error Codes](https://api-docs.deepseek.com/quick_start/error_codes/)、[Upload File](https://api-docs.deepseek.com/api/create-file/)、[Retrieve File](https://api-docs.deepseek.com/api/retrieve-file/)

## 对当前项目的实现推论

两个月是应用生命周期要求，不能直接编码成远端 `expires_after[seconds]=5184000`，因为超过官方 30 天上限。若需要保持完整两个月可用期，应在应用层保存原图及逻辑到期时间，并明确采用分段上传续接或永久文件加到期删除等生命周期策略；两者都需要实际的到期清理和失败恢复机制。这是实现选择，不是 DeepSeek 提供的 60 天 TTL 能力。[Upload File](https://api-docs.deepseek.com/api/create-file/)

项目附件仍只属于原会话；远端账户级文件权限不能替代应用的会话访问检查。图片从原字节改为文件 ID 输入会调整 ADR 0009 的图片传输约定，应同步记录该决策。[ADR 0001](adr/0001-conversation-owned-attachments.md)、[ADR 0009](adr/0009-conversation-attachment-full-context.md)
