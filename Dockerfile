FROM node:22-bookworm-slim AS frontend

ENV npm_config_registry=https://registry.npmmirror.com
WORKDIR /app/frontend
RUN npm install -g pnpm@11.25.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
WORKDIR /app

COPY requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt
COPY backend ./backend
COPY --from=frontend /app/frontend/dist ./frontend/dist

EXPOSE 8000
CMD ["uvicorn", "general_agent.app:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
