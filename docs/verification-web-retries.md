# 联网错误分级与后端重试验收

日期：2026-10-26。

## 行为与边界

- 400、403、404、413 等非临时错误直接返回；当前智能体执行中，相同工具参数跨调用 ID／模型轮次只外发一次，其他 URL 或查询保持可用。六个不同页面连续返回 404 后，仍可读取第七个有效页面。
- 401／402 及根联网预算耗尽会隐藏并拦截当前执行的搜索和抓取。主从执行各自保留失败策略和上下文，不跨回答尝试重放失败。
- 408、429、5xx 和连接／读取超时由后端最多尝试三次，采用指数退避及抖动，尊重 Retry-After；单次等待要求超过十秒时返回最终失败。模型不重开同参数的后端重试循环。
- 每次实际重试重新检查外发许可、截止时间与共享预算，并独立记录用量。退避等待可取消；未外发的预留通过条件更新终结，不覆盖已开始或已完成调用的状态。
- 最终工具错误包含 retryable=false、retry_managed_by=backend、次数、HTTP 状态及安全诊断字段；服务商原始错误正文不进入模型消息。无效响应及空正文也直接结束当前请求。
- 主回答与子回复来源默认隐藏；子 Agent 回复使用安全 Markdown 渲染。修正子回复引用锚点并验证点击后展开、定位与键盘焦点，正文粗体保持行内排版。

## 实际验证

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q --tb=short` | 338 通过、5 跳过；1 条现有 pypdf 弃用警告 |
| `.venv/bin/python -m pytest tests/search/test_failures.py tests/search/test_retry_policy.py -q --tb=short` | 39 通过，包含全量回归后补充的连续六次 404 场景 |
| `.venv/bin/python -m mypy backend tests ocr` | 159 个源文件检查通过 |
| `pnpm build`（frontend） | 类型检查与生产构建通过 |
| `pnpm exec playwright test e2e/multi-agent.spec.ts e2e/knowledge-citations.spec.ts`（frontend） | 3 通过 |
| `pnpm test:search`（frontend） | 单独复跑 1 通过；首次与 Python 全量回归同时执行时未捕捉到 0.4 秒的流式首段，失败快照中最终正文与来源入口已正常出现 |
| `git diff --check` | 通过 |

Python 集成测试使用随机独立 PostgreSQL 测试库，外部 HTTP 使用受控传输；覆盖模型输入中的错误字段、同参数重复拦截、其他请求可用、新回答重置、两个子 Agent 的独立处理、自动恢复、重试耗尽、停止和预算限制。测试不代表真实目标站点已解除抓取限制，也不保证模型会自主选择最佳替代来源。

代码与前端构建已更新，未重启正在运行的应用服务。
