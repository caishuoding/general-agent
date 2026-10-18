# LangGraph 重构交接文档

更新日期：2026-10-18

## 1. 当前状态

项目已完成从手写对话编排到 LangGraph 的整体迁移。同步聊天、流式聊天、工具调用、长期记忆、历史检索、附件与文档处理、联网授权、暂停恢复、失败收尾和消息终态现在共用同一套图结构。

本轮同时完成了目录重构、独立 Mem0 Docker 服务接入、旧业务表和旧兼容代码清理、长期记忆故障恢复、运行日志补充以及完整测试校准。

## 2. 核心架构

### 对话执行

- 对话主图负责请求分流、工具智能体、上下文准备、最终回答和统一收尾。
- 模型判断本轮不需要工具时返回 `NO_TOOL_ACTION`，图继续准备历史、长期记忆、附件或文档资料。
- 模型需要保存、查找、更正或删除长期记忆时，工具循环把结构化结果交回模型继续判断。
- 同步与流式入口只改变最终输出的传输方式，不复制业务编排。

### 联网授权

- 明确联网请求先由确定性路由识别。
- 涉及附件或长期记忆的查询在发送给搜索服务前必须经过用户确认。
- 暂停使用 LangGraph 原生中断，批准后从同一回答尝试的检查点恢复。

### 检查点

- LangGraph 官方 PostgreSQL checkpointer 使用业务 PostgreSQL 内的独立 `langgraph` schema。
- 回答尝试 ID 作为图执行身份，重试会创建新的回答尝试和新的图身份。
- 删除会话时会同步清理对应图检查点。
- 检查点不保存模型密钥、附件正文或长期记忆正文。

### 长期记忆

- 记忆正文、向量和 Mem0 内部历史只保存在独立 Mem0 服务及其 PostgreSQL 中。
- 业务后端只通过官方 Mem0 REST API 调用，不导入 Mem0 SDK，也不直接访问 Mem0 数据库。
- 业务 PostgreSQL 只保存来源、动作、版本、失败状态和幂等关系。
- Mem0 的新增、语义更新和删除事件会同步到业务账本。
- Mem0 服务异常会返回安全说明，不把底层响应正文直接展示给用户。
- 附件正文不会进入长期记忆。

## 3. 主要目录

```text
backend/general_agent/
  agent/             LangGraph 对话图和工具图
  attachments/       附件上传、解析、引用和清理
  conversation/      聊天入口、历史、文档总结和流式传输
  infrastructure/    数据库、模型调用、设置、运行日志
  memory/            长期记忆业务边界、召回、更正和删除
  prompts/           集中系统提示词
  search/            联网规划、授权和来源核验

frontend/            页面与浏览器测试
mem0/                官方 Mem0 Docker 服务与一次性配置
docs/adr/            架构决策记录
tests/               后端与接口回归测试
```

## 4. 启动与停止

### 一键启动

在项目根目录执行：

```sh
bash scripts/start.sh
```

脚本会准备依赖、构建前端、启动业务 PostgreSQL、独立 Mem0 PostgreSQL 和 Mem0 API，最后启动后端。

### VS Code

项目保留了以下任务：

- `General Agent: Database`
- `General Agent: Backend`
- `General Agent: Frontend`

后端任务使用 `--reload`，适合查看终端日志和开发调试。业务数据库迁移和图检查点初始化在后端启动时自动执行。

### 当前本机端口

| 服务 | 地址 |
| --- | --- |
| 应用后端 | `http://127.0.0.1:8000` |
| Mem0 API | `http://127.0.0.1:8888` |
| 业务 PostgreSQL | `127.0.0.1:15432` |
| Mem0 PostgreSQL | `127.0.0.1:8432` |

### 停止

- 停止后端：结束 VS Code 后端任务，或在运行后端的终端按 `Ctrl+C`。
- 停止数据库与 Mem0：`docker compose stop` 和 `docker compose -f mem0/docker-compose.yml stop`。
- 不要使用 `docker compose down -v`，该命令会删除持久化数据卷。

## 5. 配置与数据

可用环境变量：

| 变量 | 用途 |
| --- | --- |
| `DATABASE_URL` | 业务 PostgreSQL |
| `MEM0_API_URL` | Mem0 REST API 地址 |
| `MEM0_API_KEY` | 可选的 Mem0 API 认证 |
| `MEM0_USER_ID` | Mem0 用户范围，默认 `local-person` |
| `APP_DATA_DIR` | 本机配置与附件目录，默认 `.local` |
| `POSTGRES_PORT` | 业务 PostgreSQL 宿主端口 |

本机敏感配置位于 `.local/`：

- `model.json`：聊天模型配置
- `embedding.json`：嵌入配置，供 Mem0 一次性配置容器读取
- `search.json`：智谱联网配置
- `attachments/`：当前会话附件原文件
- `logs/agent.log`：智能体节点流转日志

`.local/`、依赖目录、构建产物、测试临时目录和缓存均已排除 Git。不要在提交中放入真实 API Key。

## 6. 日志

- 智能体节点流转：`.local/logs/agent.log`
- 后端启动与请求输出：`.local/backend.stderr.log`
- 后端标准输出：`.local/backend.stdout.log`

`agent.log` 记录图开始、路由结果、工具轮次、上下文准备、回答生成、收尾状态和异常堆栈。日志不记录模型密钥或完整回答正文。

日志使用滚动文件，单文件上限 5 MB，保留 5 个历史文件。

## 7. 回复与记忆边界

- 最终用户回复只陈述结论、已完成动作和实际结果。
- 不在最终回复中复述工具标识、动作名、调用参数或原始 JSON。
- 记忆工具在模型内部仍可取得具体记忆标识、版本和动作状态，以完成后续更正或删除。
- 不主动向用户播报长期记忆的新增、查询、修改或删除结果。
- 存在冲突记忆时，优先采用当前用户明确表达和较新的有效信息，不展示旧冲突版本或内部判断过程。

## 8. 验证结果

2026-10-18 最终检查：

- Python 测试 121 项，分组运行全部通过：
  - 记忆、更正、删除、召回和来源清理：34 项
  - 应用图、聊天、历史、流式和文档总结：44 项
  - 附件、搜索、设置和能力检查：43 项
- 后端与测试共 89 个文件通过类型检查。
- 前端类型检查和生产构建通过。
- Python 编译检查通过。
- Git 差异检查通过。
- 后端健康检查返回正常。
- LangGraph 检查点表已创建在业务 PostgreSQL 的 `langgraph` schema。

说明：当前机器同时运行大量测试时会短暂消耗 Windows 临时连接端口，曾出现一次数据库连接池初始化超时。单独复跑和按模块分组复跑均通过，没有业务代码失败。

## 9. 仍需注意

- 本项目设计为单个后端进程；不要使用多个 worker 共用同一业务库，否则内存中的取消状态和外部副作用协调会失去一致性。
- 更换 Mem0 的聊天或嵌入模型后，需要重启 Mem0 Compose，让一次性配置容器重新调用官方配置接口。
- Mem0 内部模型与嵌入调用不计入应用侧调用记录，真实费用仍以对应服务商账单为准。
- 浏览器端测试与实际云端模型、视觉、搜索和嵌入质量仍需使用真实凭据定期验收。
- `.test-runtime/` 是测试缓存目录，已加入忽略。当前机器上其中部分文件由系统权限创建，普通账号可能无法删除；可在管理员终端中手动清理，不影响项目运行。

## 10. 接手后的第一轮检查

```sh
git status
docker compose ps
docker compose -f mem0/docker-compose.yml ps
curl http://127.0.0.1:8000/api/health
.venv\Scripts\python.exe -m pytest tests/memory -q
.venv\Scripts\python.exe -m pytest tests/app tests/conversation -q
.venv\Scripts\python.exe -m pytest tests/attachments tests/search tests/infrastructure -q
```

前端检查：

```sh
npm.cmd run build
```

类型检查：

```sh
.venv\Scripts\python.exe -m mypy --python-version 3.14 --cache-dir .cache\mypy backend tests
```
