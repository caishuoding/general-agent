# DeepSeek Search / Fetch 接入核对

核对日期：2026-10-23。仅采用 DeepSeek 官方文档；本笔记记录协议事实，不代表已经调用真实接口。

## 接口与认证

两者是独立 JSON HTTP API，同用 DeepSeek API Key：请求头为 `Authorization: Bearer <key>` 与 `Content-Type: application/json`；无需提供聊天模型名称。地址分别为 `POST https://api.deepseek.com/v1/tools/search` 和 `POST https://api.deepseek.com/v1/tools/fetch`。[官方指南](https://api-docs.deepseek.com/guides/search/)

## Search schema

请求字段如下。[Search 参考](https://api-docs.deepseek.com/api/search/)

| 字段 | 协议约束 |
| --- | --- |
| `query` | 必填字符串；1–128 个 Unicode 字符，不能全为空白 |
| `count` | 可选整数；1–50，默认 10 |
| `ori_question` | 可选字符串或 null；提供时 1–128 字符，null 等同省略 |
| `localization` | 可选 IP 地址字符串，默认空串 |
| `need_content` | 可选布尔值，默认 false |

HTTP 200 的对象包含必填 `results` 数组；无匹配时为空数组。每项必含下列字段。[Search 参考](https://api-docs.deepseek.com/api/search/)

| 字段 | 类型及含义 |
| --- | --- |
| `title`, `url` | 字符串；标题、来源地址 |
| `snippet` | 字符串；适合界面展示的短摘要，通常 100–200 字符 |
| `summary` | 字符串；适合模型读取的长摘要，通常 600–800 字符 |
| `content` | 字符串；网页内容；未请求正文或不可用时为空串 |
| `rerank_score` | 数字；相关性 0–3；≥1.5 被视为优质结果 |
| `authority` | 整数；-1 未知，0–4 权威等级，≥3 为高权威 |
| `site_name`, `site_icon` | 字符串或 null；站点名、图标地址 |
| `date` | RFC 3339 UTC 日期字符串或 null |

查询语法支持 `site:`、`-site:`、排除词、精确短语、`lang:`、`after:`、`before:`；多个站点可用 `OR`。未知请求字段被忽略。[官方指南](https://api-docs.deepseek.com/guides/search/)

## Fetch schema 与 URL 规则

请求仅要求 `url` 字符串：非空且不超过 8192 字符，协议只能为 HTTP/HTTPS，必须含 host，禁止用户名和密码。HTTP 200 返回必填字符串 `title`、`content`；缺少标题时 title 为空串，content 是提取后的 Markdown 正文。[Fetch 参考](https://api-docs.deepseek.com/api/fetch/)

官方只保证上述入参协议；抓取仍受目标站点可用性、访问政策与 DeepSeek 安全策略限制。HTTP 404 也可能表示内容类型不支持或正文无法提取，不能仅理解成目标页面不存在。[官方指南](https://api-docs.deepseek.com/guides/search/)

## 错误、用量与限速

错误体为 `{"error":{"message":"…","type":"…","param":null,"code":"…"}}`。状态码：400 参数/请求非法，401 密钥，402 余额，403 Fetch 安全拒绝，404 Fetch 无可用正文，408 请求读取超时，413 请求过大，429 限速，500 内部错误，504 下游超时。408、429、5xx 可等待后重试；402 需充值；其余 4xx 应先修改请求。[官方指南](https://api-docs.deepseek.com/guides/search/)

按产生有效结果的 HTTP 200 请求计量；Search 空数组、Fetch 无正文、失败和超时均不计费，count 不改变单次价格。中英文页面分别列出每千次 5 元 / 0.75 美元。当前官方限速为 Search 100 QPS、Fetch 25 QPS，与密钥及模型并发独立；Fetch 另受目标域名限速。[中文指南](https://api-docs.deepseek.com/zh-cn/guides/search/)、[英文指南](https://api-docs.deepseek.com/guides/search/)

定价页对 Claude Code、Codex 等服务端调用另有临时免费说明；不要把这些集成优惠默认应用到本项目直接调用的 API。价格可能调整，应用不应把估算费用当成服务端账单；Search/Fetch 返回 schema 也没有 token usage 字段。[定价页](https://api-docs.deepseek.com/quick_start/pricing/)、[Search 参考](https://api-docs.deepseek.com/api/search/)、[Fetch 参考](https://api-docs.deepseek.com/api/fetch/)

## 对当前项目的直接影响

实现上让模型同时生成关键词化 `query` 和不超过 128 字符的 `ori_question`；请求 15 条候选以扩大召回，再依据 `rerank_score`、`authority` 和发布时间过滤、排序并限制单域名数量，最多向模型提供 8 条。摘要优先使用 `summary`，缺少时使用 `snippet`。新增 Fetch 工具可复用来源核验机制；抓取 URL 应按协议提前校验。分别保留“真实 HTTP 请求次数”与“成功且非空结果次数”，避免将空搜索当成收费调用。

项目 ADR 0008 要求工具结果作为不可信原始数据进入 tool 消息，并保留记录与来源核验；更换供应商不需要改变这个边界。[ADR 0008](adr/0008-tool-driven-web-search-and-prompt-composition.md)
