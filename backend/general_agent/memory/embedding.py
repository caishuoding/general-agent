"""为嵌入配置与后续检索索引提供一致的向量空间约束。"""

from general_agent.infrastructure.database import Database
from general_agent.infrastructure.settings import EmbeddingSettingsInput, EmbeddingSettingsStore, PublicEmbeddingSettings, StoredEmbeddingSettings, StoredSettings


class EmbeddingConfiguration:
    """管理交给独立 Mem0 服务使用的嵌入配置。"""

    def __init__(self, database: Database, store: EmbeddingSettingsStore) -> None:
        """绑定业务数据库和私有配置存储。"""
        self.database, self.store = database, store

    def public(self) -> PublicEmbeddingSettings:
        """返回不含密钥的嵌入配置；业务数据库不再持有索引。"""
        saved = self.store.read()
        return saved.public() if saved else PublicEmbeddingSettings()

    def save(self, body: EmbeddingSettingsInput, chat: StoredSettings | None) -> PublicEmbeddingSettings:
        """保存 Mem0 使用的嵌入连接配置。"""
        with self.store.lock:
            saved = self.store.prepare(body, chat)
            self.store.write(saved)
        return self.public()

    def verified(self, *, leased: bool = False) -> StoredEmbeddingSettings:
        """返回当前嵌入配置；调用方负责决定是否要求能力验证。"""
        saved = self.store.read()
        if saved is None:
            raise ValueError("请先保存并验证嵌入配置。")
        return saved

