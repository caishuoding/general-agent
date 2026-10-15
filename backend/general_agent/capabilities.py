"""分项验证模型能力，将结果固定到实际使用的配置版本。"""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from general_agent.database import Database
from general_agent.provider import ModelClient, ModelFailure, TokenUsage
from general_agent.settings import EmbeddingSettingsStore, SettingsStore, StoredEmbeddingSettings

Capability = Literal["text", "image", "tools", "embedding"]
CAPABILITIES: tuple[Capability, ...] = ("text", "image", "tools", "embedding")


class CapabilityResult(BaseModel):
    capability: Capability
    status: Literal["unverified", "passed", "failed"] = "unverified"
    revision: str | None = None
    model: str = ""
    detail: str = "尚未验证当前配置；检查会发出可能计费的请求。"
    dimensions: int | None = None
    checked_at: datetime | None = None


class CapabilityService:
    """保存独立检查结果，并在配置更新后只展示与新版本匹配的结果。"""

    def __init__(self, database: Database, model: ModelClient, settings: SettingsStore, embedding: EmbeddingSettingsStore) -> None:
        """绑定模型边界和配置来源，检查不读取会话或用户资料。"""
        self.database, self.model, self.settings, self.embedding = database, model, settings, embedding

    def results(self) -> dict[Capability, CapabilityResult]:
        """读取各项当前版本结果，旧版本或没有配置时展示未验证。"""
        results: dict[Capability, CapabilityResult] = {}
        with self.database.connect() as connection:
            for capability in CAPABILITIES:
                saved = self.embedding.read() if capability == "embedding" else self.settings.read()
                row = None
                if saved:
                    row = connection.execute("SELECT * FROM capability_checks WHERE capability = %s AND revision = %s", (capability, saved.revision)).fetchone()
                results[capability] = CapabilityResult.model_validate(row) if row else CapabilityResult(capability=capability, revision=saved.revision if saved else None, model=saved.model if saved else "")
        return results

    def check(self, capability: Capability) -> CapabilityResult:
        """检查一个已保存配置，持久化真实成功或失败以及服务已返回的用量。"""
        saved = self.embedding.read() if capability == "embedding" else self.settings.read()
        if saved is None:
            raise ValueError("请先保存对应的模型连接配置。")
        result = CapabilityResult(capability=capability, status="passed", revision=saved.revision, model=saved.model)
        usage = TokenUsage()
        try:
            if capability == "embedding":
                assert isinstance(saved, StoredEmbeddingSettings)
                response = self.model.embed(saved, ["连接验证"])
                usage = response.usage
                result.dimensions = response.dimensions
                result.detail = f"嵌入接口验证通过：模型 {response.model}，维度 {response.dimensions}。"
            else:
                usage = self.model.probe(saved, capability)
                result.detail = {"text": "文字接口已返回有效回答。", "image": "图片输入已被正确识别；本次仅验证基础图片能力。", "tools": "工具调用结构与参数验证通过。"}[capability]
        except ModelFailure as failure:
            result.status, result.detail, usage = "failed", str(failure), failure.usage
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO model_calls (id, model, purpose, status, prompt_tokens, completion_tokens, total_tokens) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uuid4(), saved.model, "capability_" + capability, "complete" if result.status == "passed" else "failed", usage.prompt_tokens, usage.completion_tokens, usage.total_tokens),
            )
            row = connection.execute(
                "INSERT INTO capability_checks (capability, revision, model, status, detail, dimensions) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (capability, revision) DO UPDATE SET status = EXCLUDED.status, detail = EXCLUDED.detail, dimensions = EXCLUDED.dimensions, checked_at = CURRENT_TIMESTAMP RETURNING *",
                (capability, saved.revision, saved.model, result.status, result.detail, result.dimensions),
            ).fetchone()
            assert row is not None
            return CapabilityResult.model_validate(row)
