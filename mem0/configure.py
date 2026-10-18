"""在官方 Mem0 服务启动后注入项目现有的独立模型配置。"""

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx


def read_settings(path: str) -> dict[str, Any]:
    """读取项目私有配置文件并返回字典；正文和密钥不写入日志。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_config(model: dict[str, Any], embedding: dict[str, Any]) -> dict[str, Any]:
    """构造官方 Mem0 配置，将聊天模型和嵌入模型保持为两套独立凭据。"""
    dimensions = int(embedding["dimensions"])
    return {
        "version": "v1.1",
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "host": "mem0-postgres",
                "port": 5432,
                "dbname": "postgres",
                "user": "postgres",
                "password": "local-mem0-only",
                "collection_name": "memories",
                "embedding_model_dims": dimensions,
            },
        },
        "llm": {
            "provider": "openai",
            "config": {
                "api_key": model["api_key"],
                "openai_base_url": model["base_url"],
                "model": model["model"],
                "temperature": 0.2,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "api_key": embedding["api_key"],
                "openai_base_url": embedding["base_url"],
                "model": embedding["model"],
                "embedding_dims": dimensions,
            },
        },
        "history_db_path": "/app/history/history.db",
    }


def configure() -> None:
    """等待 Mem0 API 就绪后提交配置，超时或失败时让容器明确失败。"""
    url = os.environ.get("MEM0_URL", "http://mem0:8000")
    config = build_config(read_settings("/settings/model.json"), read_settings("/settings/embedding.json"))
    with httpx.Client(timeout=15.0) as client:
        for _ in range(60):
            try:
                response = client.post(f"{url}/configure", json=config)
                response.raise_for_status()
                print("Mem0 configuration applied.")
                return
            except httpx.HTTPError:
                time.sleep(1)
    raise RuntimeError("Mem0 服务未能在 60 秒内完成配置。")


if __name__ == "__main__":
    configure()
