#!/usr/bin/env bash
# 准备项目依赖、持久化数据库和前端构建，并在本机启动应用。
set -euo pipefail
GENERAL_AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$GENERAL_AGENT_ROOT"
GENERAL_AGENT_RUNTIME="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies"
export PATH="$PATH:$HOME/.local/bin:$HOME/.docker/bin:$GENERAL_AGENT_RUNTIME/node/bin:$GENERAL_AGENT_RUNTIME/bin/fallback"
GENERAL_AGENT_PYTHON="${GENERAL_AGENT_PYTHON:-3.12}"
if [[ "$GENERAL_AGENT_PYTHON" == "3.12" && -x "$GENERAL_AGENT_RUNTIME/python/bin/python3" ]]; then
  GENERAL_AGENT_PYTHON="$GENERAL_AGENT_RUNTIME/python/bin/python3"
fi
uv sync --locked --python "$GENERAL_AGENT_PYTHON"
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend run build
docker compose up -d --wait
exec .venv/bin/uvicorn general_agent.app:create_app --factory --app-dir backend --host 127.0.0.1 --port 8000
