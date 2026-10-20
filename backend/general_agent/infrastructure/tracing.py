"""加载本机 LangSmith 配置并为原始模型协议生成不含连接密钥的追踪数据。"""

import json
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel
from langsmith import utils as langsmith_utils


def configure_tracing(directory: Path) -> None:
    """加载私密追踪配置并记录启用状态和项目名，显式环境优先且日志不含密钥。"""
    path = directory / "langsmith.json"
    values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT",
                 "LANGSMITH_ENDPOINT", "LANGSMITH_WORKSPACE_ID", "LANGSMITH_AGENT_ID",
                 "LANGSMITH_AGENT_ENVIRONMENT"):
        if name in values:
            os.environ.setdefault(name, str(values[name]))
    # SDK 的重载类型未暴露 lru_cache 方法，刷新导入期间缓存的环境配置。
    cast(Any, langsmith_utils.get_env_var).cache_clear()
    langsmith_utils.get_tracer_project.cache_clear()
    logging.getLogger(__name__).info(
        "event=tracing_configured enabled=%s project=%s agent=%s environment=%s",
        langsmith_utils.tracing_is_enabled(), langsmith_utils.get_tracer_project(return_default_value=False),
        os.environ.get("LANGSMITH_AGENT_ID"), os.environ.get("LANGSMITH_AGENT_ENVIRONMENT"),
    )


def model_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """只发送模型名、消息和工具参数，排除客户端与包含 API Key 的配置对象。"""
    settings = inputs.get("settings")
    result = {key: inputs[key] for key in ("messages", "tools", "texts", "json_mode") if key in inputs}
    result["model"] = settings.model if settings is not None else None
    return result


def model_outputs(output: BaseModel) -> dict[str, Any]:
    """序列化已验证的模型结果，并将真实 token 用量映射到 LangSmith 标准字段。"""
    result = output.model_dump(mode="json")
    usage = result.pop("usage", None)
    if usage:
        result["usage_metadata"] = usage_metadata(usage)
    return result


def usage_metadata(usage: dict[str, Any]) -> dict[str, int]:
    """转换服务商用量字段，未知用量保持缺失，不估算 token 数量。"""
    fields = {"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens", "total_tokens": "total_tokens"}
    return {target: usage[source] for source, target in fields.items() if usage.get(source) is not None}


def stream_outputs(chunks: Sequence[Any]) -> dict[str, Any]:
    """汇总流式文字、推理、工具分片和最后一次真实用量，不改变调用方消费行为。"""
    result: dict[str, Any] = {
        "content": "".join(chunk.content for chunk in chunks),
        "reasoning_content": "".join(chunk.reasoning_content for chunk in chunks),
        "tool_call_deltas": [chunk.tool_call.model_dump(mode="json") for chunk in chunks if chunk.tool_call is not None],
    }
    for chunk in reversed(chunks):
        if chunk.usage is not None:
            result["usage_metadata"] = usage_metadata(chunk.usage.model_dump())
            break
    return result
