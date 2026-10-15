"""为嵌入配置与后续检索索引提供一致的向量空间约束。"""

from general_agent.database import Database
from general_agent.settings import EmbeddingSettingsInput, EmbeddingSettingsStore, PublicEmbeddingSettings, StoredEmbeddingSettings, StoredSettings


class EmbeddingIndexConflict(ValueError):
    """已有索引不允许直接更换向量空间，必须由重建流程协调。"""


class EmbeddingConfiguration:
    """统一嵌入配置修改、已验证配置获取和索引身份登记。"""

    def __init__(self, database: Database, store: EmbeddingSettingsStore) -> None:
        """绑定真实索引登记与私有配置，所有写操作使用同一数据库锁。"""
        self.database, self.store = database, store

    def public(self) -> PublicEmbeddingSettings:
        """返回无密钥配置与已有索引数量，供界面解释重建限制。"""
        saved = self.store.read()
        result = saved.public() if saved else PublicEmbeddingSettings()
        with self.database.connect() as connection:
            row = connection.execute("SELECT count(*) AS count FROM embedding_indexes").fetchone()
            assert row is not None
            result.index_count = row["count"]
        return result

    def save(self, body: EmbeddingSettingsInput, chat: StoredSettings | None) -> PublicEmbeddingSettings:
        """拒绝与已登记索引不同的地址、模型或维度，允许轮换同空间凭据。"""
        with self.database.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(91402703)")
            with self.store.lock:
                saved = self.store.prepare(body, chat)
                incompatible = connection.execute(
                    "SELECT name FROM embedding_indexes WHERE base_url <> %s OR model <> %s OR dimensions <> %s LIMIT 1",
                    (saved.base_url, saved.model, saved.dimensions),
                ).fetchone()
                if incompatible:
                    raise EmbeddingIndexConflict("已有检索索引，更换嵌入服务、模型或维度需要重建索引。当前重建功能尚未启用，配置未修改。")
                self.store.write(saved)
        return self.public()

    def verified(self) -> StoredEmbeddingSettings:
        """向检索消费者提供当前已通过验证的配置，未配置或旧版本结果不得使用。"""
        saved = self.store.read()
        if saved is None:
            raise ValueError("请先保存并验证嵌入配置。")
        with self.database.connect() as connection:
            row = connection.execute("SELECT status FROM capability_checks WHERE capability = 'embedding' AND revision = %s", (saved.revision,)).fetchone()
            if row is None or row["status"] != "passed":
                raise ValueError("当前嵌入配置尚未验证通过，请在设置中重新检查。")
        return saved

    def bind_index(self, name: str, saved: StoredEmbeddingSettings) -> None:
        """为新索引登记已验证的向量空间，拒绝过期配置或复用不同模型的已有索引名。"""
        with self.database.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(91402703)")
            current = self.verified()
            if current.revision != saved.revision:
                raise EmbeddingIndexConflict("嵌入配置已更新，请重新获取已验证配置。")
            row = connection.execute("SELECT base_url, model, dimensions FROM embedding_indexes WHERE name = %s", (name,)).fetchone()
            identity = (saved.base_url, saved.model, saved.dimensions)
            if row and (row["base_url"], row["model"], row["dimensions"]) != identity:
                raise EmbeddingIndexConflict("该检索索引需要重建，禁止混用不同嵌入模型或维度。")
            connection.execute("INSERT INTO embedding_indexes (name, base_url, model, dimensions) VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING", (name, *identity))
