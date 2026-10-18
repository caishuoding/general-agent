# 智谱 Web Search 替换验证（2026-10-17）

联网搜索已由 Tavily 切换为智谱 Web Search。独立本机配置使用 `https://open.bigmodel.cn/api/paas/v4`、固定身份 `zhipu-search-std` 和 `search_std` 引擎；密钥保存在 Git 忽略的 `.local/search.json`，公开读取只返回配置状态。

## 协议与边界

- 对照智谱官方文档，请求使用 `POST /web_search`、Bearer 鉴权、`search_query`、`search_engine=search_std`、`search_intent=false`、`count=5`、`search_recency_filter=noLimit` 和 `content_size=medium`。
- 查询最长 70 字符；响应只接受 `search_result` 数组及其中安全的 HTTP/HTTPS `link`，继续忽略供应商正文中的指令。
- 普通聊天不联网；引用长期记忆或附件生成的查询仍需展示完整文本并取得当前修订的一次性批准。
- 智谱响应不返回 Tavily credits。预算数据库沿用现有 `credits` 数量列保存实际发出的搜索请求数，界面明确按人民币元/次配置；收到 HTTP 响应后计一次，连接失败或超时保持费用未知。
- 新迁移 021 更新已有搜索确认卡的供应商用途文案。旧 `tavily-basic` 配置不会被当作智谱配置使用。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 真实智谱 `search_std` 请求 | 成功；返回 5 条结构化结果及请求标识，不含个人信息 |
| `.venv\\Scripts\\python.exe -m mypy --no-incremental --platform linux` | 79 个源文件通过 |
| Python `compileall` | 通过 |
| `pnpm --dir frontend build` | TypeScript 与 Vite 生产构建通过 |
| PostgreSQL 17 + pgvector 集成回归 | 19 项中 17 项通过；2 项既有 Windows 平台差异失败，均与智谱搜索协议无关 |
| `pnpm --dir frontend test:search` | 1 项 Playwright 搜索用例通过 |
| `git diff --check` | 通过 |

受控测试已更新为智谱请求和响应形状，包括配置身份、请求参数、来源过滤、预算请求计数、HTTP 失败、停止、外发批准及浏览器标签。Docker Hub 直连两次超时后，通过 `mirror.gcr.io` 拉取并标记同一固定摘要的 PostgreSQL 17 基础镜像；原 Compose 构建随后成功，项目 PostgreSQL 17 + pgvector 0.8.7 在隔离的 15432 端口健康运行。搜索与总结的 19 项集成测试中 17 项通过；两个失败分别是 Windows 不支持 POSIX `0600` 文件模式断言，以及既有的 Windows 混合 PDF 引用行为，均未落在智谱搜索协议或搜索浏览器流程上。机器上已有的 5432 PostgreSQL 归属不明，未对其执行项目迁移。

真实请求证明当前密钥、Bearer 鉴权和协议可用，不代表来源质量、长期稳定性或账单金额已经完整核对。官方文档在验证时列出的 `search_std` 价格为 0.01 元/次，实际价格以智谱账单为准。
