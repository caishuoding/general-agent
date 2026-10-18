Status: ready-for-agent

# 19: 将联网搜索切换为智谱 Web Search

**What to build:** 用智谱 Web Search `search_std` 替换 Tavily，保留明确联网触发、个人资料外发确认、来源核验、停止重试和预算边界。

Completion: complete

- [x] 搜索配置使用独立智谱地址与密钥，旧 Tavily 配置不会被静默复用。
- [x] 后端按智谱官方 `/api/paas/v4/web_search` 协议发送 Bearer 请求，并解析 `search_result` 中的标题、链接与摘要。
- [x] 查询限制为 70 字符，每次最多返回五条，仍只在用户明确要求联网后调用。
- [x] 记忆或附件派生查询继续显示具体外发内容，批准前不发送给智谱。
- [x] `search_std` 按实际发出的请求次数进入预算计量，界面展示人民币元/次单价。
- [x] 设置界面、受控替身、浏览器用例、规格与使用说明同步更新。
- [x] 真实智谱请求验证密钥、鉴权、请求与响应协议，不发送用户资料。

## Comments

2026-10-17：实现及真实智谱协议验证完成。严格 Mypy、Python 编译、前端生产构建、Playwright 搜索用例和 `git diff --check` 通过。固定摘要镜像经镜像站恢复后，项目 PostgreSQL 17 + pgvector 0.8.7 集成回归完成：19 项中 17 项通过，两个失败为与本任务无关的既有 Windows 平台差异，见 [验证记录](../../../docs/verification-zhipu-search.md)。
