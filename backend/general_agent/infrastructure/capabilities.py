"""分项验证模型能力，将结果固定到实际使用的配置版本。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.provider import ModelFailure
from general_agent.infrastructure.settings import EmbeddingSettingsStore, SettingsStore, StoredEmbeddingSettings, StoredSettings

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

    def __init__(self, database: Database, model: CallGateway, settings: SettingsStore, embedding: EmbeddingSettingsStore) -> None:
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

    def check(self, capability: Capability, operation: OperationContext, saved: StoredSettings) -> CapabilityResult:
        """检查调用方固定的配置快照，避免验证期间配置变更导致批准范围错配。"""
        result = CapabilityResult(capability=capability, status="passed", revision=saved.revision, model=saved.model)
        try:
            if capability == "embedding":
                assert isinstance(saved, StoredEmbeddingSettings)
                response = self.model.bind(operation).embed(saved, ["连接验证"])
                result.dimensions = response.dimensions
                result.detail = f"嵌入接口验证通过：模型 {response.model}，维度 {response.dimensions}。"
            else:
                self.model.bind(operation).probe(saved, capability)
                result.detail = {"text": "文字接口已返回有效回答。", "image": "图片输入已被正确识别；本次仅验证基础图片能力。", "tools": "工具调用结构与参数验证通过。"}[capability]
        except ModelFailure as failure:
            result.status, result.detail = "failed", str(failure)
        with self.database.connect() as connection:
            row = connection.execute(
                "INSERT INTO capability_checks (capability, revision, model, status, detail, dimensions) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (capability, revision) DO UPDATE SET status = EXCLUDED.status, detail = EXCLUDED.detail, dimensions = EXCLUDED.dimensions, checked_at = CURRENT_TIMESTAMP RETURNING *",
                (capability, saved.revision, saved.model, result.status, result.detail, result.dimensions),
            ).fetchone()
            assert row is not None
            return CapabilityResult.model_validate(row)
