"""通过真实 SDK 边界验证模型追踪的密钥过滤、嵌套关系和流式结果。"""

import json
import os
from pathlib import Path

import httpx
import pytest
from langsmith import Client, trace, tracing_context
from langsmith.run_trees import RunTree

from general_agent.infrastructure.provider import ModelClient
from general_agent.infrastructure.settings import StoredSettings
from general_agent.infrastructure.tracing import configure_tracing


def test_config_environment_overrides_private_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式关闭追踪的环境变量优先于文件，私密配置仅补充缺失项。"""
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    (tmp_path / "langsmith.json").write_text(json.dumps({
        "LANGSMITH_TRACING": "true", "LANGSMITH_PROJECT": "general-agent",
    }))
    configure_tracing(tmp_path)
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGSMITH_PROJECT"] == "general-agent"


def test_private_config_addresses_agent_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """智能体环境绑定不注入同名独立项目，SDK 将链路路由到 Local 环境。"""
    from langsmith.address import address_from_env

    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in ("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT", "LANGSMITH_AGENT_ID", "LANGSMITH_AGENT_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "langsmith.json").write_text(json.dumps({
        "LANGSMITH_AGENT_ID": "general-agent", "LANGSMITH_AGENT_ENVIRONMENT": "local",
    }))
    configure_tracing(tmp_path)
    assert address_from_env() == {"kind": "AGENT", "id": "general-agent", "environment": "LOCAL"}
    assert "LANGSMITH_PROJECT" not in os.environ


def test_model_trace_filters_credentials_and_keeps_stream_usage() -> None:
    """模型 span 嵌套到父链路，输入不含密钥，流式汇总保留文字、工具参数与用量。"""
    runs: list[RunTree] = []

    def collect(run: RunTree) -> None:
        """收集 SDK 完成后的本地 span，避免测试向云端写入。"""
        runs.append(run)

    def provider(request: httpx.Request) -> httpx.Response:
        """按协议返回受控 SSE，包括工具调用和真实用量尾帧。"""
        frames = [
            {"choices": [{"delta": {"content": "连接成功"}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {
                "name": "search", "arguments": "{}"}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")

    model = ModelClient(httpx.MockTransport(provider))
    settings = StoredSettings(base_url="https://model.example/v1", model="fixture", api_key="never-upload-this-key")
    client = Client(api_url="http://localhost:1", api_key="local-test", info={})
    with tracing_context(enabled="local", client=client), trace("conversation", inputs={}) as parent:
        chunks = list(model.stream(settings, [{"role": "user", "content": "验证"}],
                                   langsmith_extra={"on_end": collect}))
    assert "".join(chunk.content for chunk in chunks) == "连接成功"
    assert len(runs) == 1
    run = runs[0]
    assert run.parent_run_id == parent.id
    assert run.run_type == "llm"
    assert run.inputs == {"model": "fixture", "messages": [{"role": "user", "content": "验证"}], "tools": None}
    assert "never-upload-this-key" not in json.dumps(run.inputs)
    assert run.outputs is not None
    assert run.outputs["usage_metadata"] == {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}
    assert run.outputs["content"] == "连接成功"
    assert run.outputs["tool_call_deltas"][0]["name"] == "search"
