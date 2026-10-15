"""只登记本应用记忆集合，以空间租约协调业务发布与索引切换。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from time import sleep
from uuid import uuid4

from general_agent.database import Database
from general_agent.settings import StoredEmbeddingSettings

SPACE_LOCK=91402718


class MemoryMaintenance(ValueError):
    """记忆空间正在维护，新的写入应在改变事实版本前拒绝并允许重试。"""


class MemorySpaces:
    """可信集合注册表，不根据用户输入或任意数据库表名寻找向量。"""

    def __init__(self, database: Database) -> None:
        """绑定持久登记；所有进程共享数据库租约。"""
        self.database=database
        self.held: ContextVar[bool]=ContextVar("memory_space_lease",default=False)

    def maintaining(self) -> bool:
        """发布后仅清理旧表不影响查询，其余未完成阶段暂停新记忆业务。"""
        with self.database.connect() as connection:
            return connection.execute("SELECT id FROM embedding_rebuilds WHERE status NOT IN ('complete','cancelled') AND phase<>'cleanup' LIMIT 1").fetchone() is not None

    @contextmanager
    def lease(self, *, exclusive: bool = False, maintenance: bool = False) -> Iterator[None]:
        """维护期先即时拒绝新业务，再持会话锁到完整发布；二次核验防止准入竞态。"""
        if self.held.get():
            yield
            return
        if not maintenance and self.maintaining():
            raise MemoryMaintenance('长期记忆索引正在重建，完成或取消后可重试；当前事实未修改。')
        with self.database.connect() as connection:
            connection.autocommit=True
            if exclusive:
                # 不排队独占锁，避免等待慢云请求时挡住本地查看及立即删除的 shared lease。
                while True:
                    acquired=connection.execute('SELECT pg_try_advisory_lock(%s) AS ok',(SPACE_LOCK,)).fetchone()
                    if acquired and acquired['ok']:
                        break
                    sleep(.02)
            else:
                connection.execute('SELECT pg_advisory_lock_shared(%s)',(SPACE_LOCK,))
            if not maintenance and self.maintaining():
                raise MemoryMaintenance('长期记忆索引正在重建，暂时停止召回和记忆写入；完成或取消后可重试。')
            token=self.held.set(True)
            try:
                yield
            finally:
                self.held.reset(token)

    def registered(self) -> list[dict[str, Any]]:
        """初始化历史唯一逻辑集合的登记，再返回所有尚未移除的受控空间。"""
        with self.database.connect() as connection:
            connection.execute('SELECT pg_advisory_xact_lock(91402703)')
            current=connection.execute("SELECT id FROM embedding_spaces WHERE status='active'").fetchone()
            if current is None:
                initial=connection.execute("SELECT * FROM embedding_indexes WHERE name='personal_memories'").fetchone()
                if initial:
                    connection.execute("INSERT INTO embedding_spaces(id,collection_name,base_url,model,dimensions,status) VALUES(%s,'personal_memories',%s,%s,%s,'active') ON CONFLICT(collection_name) DO NOTHING",(uuid4(),initial['base_url'],initial['model'],initial['dimensions']))
            return connection.execute("SELECT * FROM embedding_spaces WHERE status<>'removed' ORDER BY created_at,id").fetchall()

    def active(self) -> dict[str, Any] | None:
        """只从本应用登记解析当前空间，不因私有配置文件换入而提前切表。"""
        return next((row for row in self.registered() if row['status']=='active'),None)

    def binding(self, settings: StoredEmbeddingSettings) -> str:
        """核验模型、地址和维度再返回活动物理名，禁止新向量进入旧空间。"""
        current=self.active()
        if current is None:
            return 'personal_memories'
        if (current['base_url'],current['model'],current['dimensions'])!=(settings.base_url,settings.model,settings.dimensions):
            raise MemoryMaintenance('嵌入配置与活动索引不一致，正在恢复切换；暂时不能查询。')
        return str(current['collection_name'])

    def local_settings(self) -> StoredEmbeddingSettings | None:
        """查看和删除采用登记的活动空间，无需密钥、预算或新目标配置可用。"""
        current=self.active()
        return StoredEmbeddingSettings(base_url=current['base_url'],model=current['model'],dimensions=current['dimensions'],api_key='') if current else None
