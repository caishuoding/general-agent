# 个人对话智能体

当前实现任务 01：本机 Web 文字对话、模型配置、会话持久化和调用用量记录。长期记忆、附件、流式回答、搜索和预算控制仍按后续任务实现；记忆页面会明确显示尚未启用。

## 启动

需要 Docker Desktop（已启动）、Python 3.12+、uv、Node.js 22.12+ 和 pnpm 11.25.0。项目锁定 Python 与前端依赖，以及 PostgreSQL 镜像摘要。

在项目根目录执行：

```sh
bash scripts/start.sh
```

脚本安装锁定的依赖、构建前端、启动 PostgreSQL，随后在前台启动应用。若电脑安装了 Codex 自带运行时，脚本也会尝试使用它；可通过 `GENERAL_AGENT_PYTHON` 指定其他 Python。浏览器打开 [本机应用](http://127.0.0.1:8000)。

在设置中输入服务商提供的 API 基础地址（通常以 `/v1` 结尾）、文字模型名称和 API 密钥，然后新建会话并发送消息。接口使用 OpenAI 兼容的 Chat Completions；保存配置仅表示已保存，不代表服务能力已验证。云端地址使用 HTTPS，本机服务允许回环 HTTP。更换服务地址需重新填写密钥；同一地址留空密钥会保留原密钥。

此版本一次性返回完整文字回答。设置页展示最近调用的 token 用量，缺失值显示为未知；尚未估算费用或执行预算暂停。真实调用按你的服务商规则计费。

按 Ctrl+C 停止应用。停止数据库可执行 `docker compose stop`；再次启动后数据仍保留。数据库默认仅监听 `127.0.0.1:15432`，应用仅监听 `127.0.0.1:8000`。

## 数据与配置

| 内容 | 位置 |
| --- | --- |
| 会话、消息、调用记录、结构版本 | PostgreSQL 的 `general-agent_postgres_data` 持久化卷 |
| 模型地址、名称、密钥 | 项目内 `.local/model.json`，新建文件权限为仅当前用户可读写 |
| 前端构建 | `frontend/dist/`，可重新生成 |
| 规格与任务 | `.scratch/general-agent/` |

`.local/`、环境文件、依赖和构建产物均已排除 Git。当前不写入附件，也未创建 Mem0 数据。密钥仅由后端读取，配置接口不回传完整密钥；模型错误正文不会直接展示。

可用环境变量：`DATABASE_URL` 指定 PostgreSQL；`APP_DATA_DIR` 指定本机配置目录；`POSTGRES_PORT` 修改 Compose 的宿主端口。修改数据库端口时，同步设置 `DATABASE_URL`。Compose 内的密码仅用于这个本机开发实例。

数据库结构在应用启动时按版本迁移，已应用的迁移不会重复执行。请保留数据库卷；`docker compose down -v` 会删除其数据。

## 开发与检查

```sh
uv sync --locked
docker compose up -d --wait
uv run mypy
uv run pytest tests/test_chat.py -q
pnpm --dir frontend typecheck
pnpm --dir frontend build
pnpm --dir frontend exec playwright install chromium
pnpm --dir frontend test:e2e
```

完整 Python 测试使用 `uv run pytest -q`。默认测试连接本机 PostgreSQL 的管理库，每个测试创建随机命名的独立数据库，结束后只删除它创建的数据库；可用 `TEST_DATABASE_URL` 指定专用测试实例。浏览器测试使用临时配置、独立数据库和本机受控模型，不读取你的模型密钥，不调用付费服务。

开发热更新可分别运行 `uv run uvicorn general_agent.app:create_app --factory --app-dir backend --host 127.0.0.1 --port 8000 --reload` 和 `pnpm --dir frontend dev`，浏览器访问 Vite 提供的本机地址。

测试遵循应用对外接口边界，使用真实 PostgreSQL；测试用模型替身只替换外部 HTTP 响应。实际服务兼容性需填写自己的模型配置后验证，替身通过不等同于真实接口已验收。

参考：[FastAPI 外部依赖测试](https://fastapi.tiangolo.com/advanced/testing-dependencies/)、[Psycopg 事务管理](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)、[Vite 使用说明](https://vite.dev/guide/)、[pnpm 构建脚本配置](https://pnpm.io/settings/build#allowbuilds)。
