#!/usr/bin/env bash
# 构建完整 PPT Master 运行镜像和公网出口；不读取或打印应用凭据。
set -euo pipefail
task_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$task_repo_root"
export SKILL_HOST_DATA_DIR="${SKILL_HOST_DATA_DIR:-$task_repo_root/.local}"
docker compose -f compose.yaml -f compose.skills.yaml build skill-image skill-egress
docker compose -f compose.yaml -f compose.skills.yaml up -d --no-deps skill-egress
