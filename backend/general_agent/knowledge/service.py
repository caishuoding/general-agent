"""管理知识库权限、会话选择及可恢复的文档后台任务。"""

from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import shutil
from typing import Any
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb

from general_agent.attachments.service import AttachmentService
from general_agent.attachments.knowledge import ChunkPolicy, configured_chunk_policy
from general_agent.attachments.upload import UploadedFile
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.settings import EmbeddingSettingsStore
from general_agent.attachments.document_images import VISION_PROMPT_VERSION

LOGGER = logging.getLogger(__name__)


class KnowledgeService:
    """对外提供账号范围内的知识库管理，任务持久化于独立文档记录。"""

    def __init__(self, database: Database, directory: Path, attachments: AttachmentService,
                 calls: CallGateway, transport: httpx.BaseTransport | None = None) -> None:
        """复用公共切片策略和模型连接，文件与后台队列保持独立。"""
        self.database, self.directory = database, directory / 'knowledge'
        self.files = attachments.files
        self.calls, self.transport, self.settings_directory = calls, transport, directory
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='knowledge')

    @property
    def policy(self) -> ChunkPolicy:
        """按当前 LLM 创建切片策略，后台任务在开始时固定本次快照。"""
        return configured_chunk_policy(self.settings_directory)

    def recover(self) -> None:
        """恢复中断或结构规则变化的任务，人工清空和单独切换 LLM 不触发重切。"""
        policy = self.policy
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in self.directory.glob('*.upload'):
            path.unlink(missing_ok=True)
        with self.database.connect() as connection:
            embedding = EmbeddingSettingsStore(self.settings_directory).read()
            connection.execute(
                "UPDATE knowledge_documents d SET status='processing',stage='queued',attempt=%s "
                "FROM knowledge_bases b WHERE b.id=d.knowledge_base_id AND NOT b.deleting AND d.status='ready' "
                "AND COALESCE(d.parse_info->>'index_cleared','false')<>'true' "
                "AND (COALESCE(d.parse_info->>'chunk_layout',d.chunk_policy) IS DISTINCT FROM %s OR (%s::text IS NOT NULL AND d.index_revision IS DISTINCT FROM %s) "
                "OR (EXISTS(SELECT 1 FROM knowledge_document_images i WHERE i.document_id=d.id) AND d.parse_info->>'vision_rules' IS DISTINCT FROM %s) "
                "OR (lower(d.filename) LIKE '%%.docx' AND d.parse_info->>'text_source' IS DISTINCT FROM 'word_native_markdown_v3') "
                "OR (lower(d.filename) LIKE '%%.pdf' AND d.parse_info->>'text_source' IS DISTINCT FROM 'pdf_tables_v2'))",
                (uuid4(), policy.layout_signature, embedding.revision if embedding else None,
                 embedding.revision if embedding else None, VISION_PROMPT_VERSION),
            )
            rows = connection.execute(
                "SELECT d.id FROM knowledge_documents d JOIN knowledge_bases b ON b.id=d.knowledge_base_id "
                "WHERE d.status IN ('processing','deleting') OR b.deleting"
            ).fetchall()
        for row in rows:
            self.schedule(row['id'])
        self.cleanup_bases()

    def close(self) -> None:
        """关闭任务入口并等待已经开始的任务，避免停机后线程继续使用数据库。"""
        self.executor.shutdown(wait=True, cancel_futures=True)

    def list_bases(self, account_id: UUID) -> list[dict[str, Any]]:
        """列出自己的知识库和待清理对象，保留整库删除失败后的重试入口。"""
        with self.database.connect() as connection:
            return connection.execute(
                'SELECT b.id,b.name,b.deleting,b.created_at,COUNT(d.id) FILTER(WHERE d.status<>\'deleting\') AS document_count '
                'FROM knowledge_bases b LEFT JOIN knowledge_documents d ON d.knowledge_base_id=b.id '
                'WHERE b.user_id=%s GROUP BY b.id ORDER BY b.created_at,b.id', (account_id,),
            ).fetchall()

    def guard(self, connection: Any, base_id: UUID, account_id: UUID, *, lock: bool = False) -> dict[str, Any]:
        """核验账号对未删除知识库的所有权，写入路径可持有行锁阻止并发删除。"""
        row = connection.execute(
            'SELECT * FROM knowledge_bases WHERE id=%s AND user_id=%s AND NOT deleting' + (' FOR UPDATE' if lock else ''),
            (base_id, account_id),
        ).fetchone()
        if row is None:
            raise LookupError('知识库不存在或已删除。')
        return dict(row)

    def create(self, account_id: UUID, name: str) -> dict[str, Any]:
        """为本账号建立独立知识库，名称由 API 校验。"""
        with self.database.connect() as connection:
            row = connection.execute('INSERT INTO knowledge_bases(id,user_id,name) VALUES(%s,%s,%s) RETURNING id,name,created_at',
                                     (uuid4(), account_id, name)).fetchone()
        assert row is not None
        return dict(row)

    def rename(self, base_id: UUID, account_id: UUID, name: str) -> dict[str, Any]:
        """只重命名本账号仍有效的知识库。"""
        with self.database.connect() as connection:
            self.guard(connection, base_id, account_id, lock=True)
            row = connection.execute('UPDATE knowledge_bases SET name=%s WHERE id=%s RETURNING id,name,created_at', (name, base_id)).fetchone()
        assert row is not None
        return dict(row)

    def selected(self, conversation_id: UUID, account_id: UUID) -> list[dict[str, Any]]:
        """读取本会话选择的有效知识库，任何跨账号关系均不能返回。"""
        from general_agent.attachments.tools import DocumentTools
        with self.database.connect() as connection:
            DocumentTools.guard(connection, conversation_id, account_id)
            return connection.execute(
                'SELECT b.id,b.name FROM conversation_knowledge_bases s JOIN knowledge_bases b ON b.id=s.knowledge_base_id '
                'WHERE s.conversation_id=%s AND b.user_id=%s AND NOT b.deleting ORDER BY b.created_at,b.id',
                (conversation_id, account_id),
            ).fetchall()

    def select(self, conversation_id: UUID, account_id: UUID, identifiers: list[UUID]) -> list[dict[str, Any]]:
        """原子替换会话选择，未授权或已删除的任一知识库使整次更新失败。"""
        with self.database.connect() as connection:
            conversation = connection.execute('SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting FOR UPDATE',
                                              (conversation_id, account_id)).fetchone()
            if conversation is None:
                raise LookupError('会话不存在。')
            for identifier in sorted(set(identifiers)):
                self.guard(connection, identifier, account_id, lock=True)
            connection.execute('DELETE FROM conversation_knowledge_bases WHERE conversation_id=%s', (conversation_id,))
            for identifier in set(identifiers):
                connection.execute('INSERT INTO conversation_knowledge_bases VALUES(%s,%s)', (conversation_id, identifier))
        return self.selected(conversation_id, account_id)

    def documents(self, base_id: UUID, account_id: UUID) -> list[dict[str, Any]]:
        """返回原始阶段计数与失败说明，不暴露私有路径、远端文件或模型凭据。"""
        with self.database.connect() as connection:
            self.guard(connection, base_id, account_id)
            return connection.execute(
                'SELECT id,filename,size,status,stage,completed,total,error,warning,parse_info,updated_at,created_at '
                'FROM knowledge_documents WHERE knowledge_base_id=%s ORDER BY created_at,id', (base_id,),
            ).fetchall()

    def register(self, base_id: UUID, account_id: UUID, uploaded: list[UploadedFile]) -> list[dict[str, Any]]:
        """上传成功后原子登记独立文档，失败时回收本次移动的原件。"""
        destinations: list[Path] = []
        rows: list[dict[str, Any]] = []
        try:
            if any(Path(item.filename).suffix.lower() not in {'.txt', '.md', '.markdown', '.pdf', '.docx'} for item in uploaded):
                raise ValueError('知识库支持 PDF、DOCX、TXT 和 Markdown 文档。')
            with self.database.connect() as connection:
                self.guard(connection, base_id, account_id, lock=True)
                for item in uploaded:
                    identifier = uuid4()
                    folder = self.directory / str(identifier)
                    folder.mkdir(mode=0o700)
                    destinations.append(folder)
                    item.path.replace(folder / 'original')
                    row = connection.execute(
                        "INSERT INTO knowledge_documents(id,knowledge_base_id,filename,size,status,attempt) "
                        "VALUES(%s,%s,%s,%s,'processing',%s) RETURNING id,filename,status,stage,completed,total",
                        (identifier, base_id, item.filename, item.size, uuid4()),
                    ).fetchone()
                    assert row is not None
                    rows.append(dict(row))
        except BaseException:
            for folder in destinations:
                shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            for item in uploaded:
                item.path.unlink(missing_ok=True)
        return rows

    def schedule(self, document_id: UUID) -> None:
        """把已持久化任务交给有界后台线程，不让 HTTP 响应等待视觉处理。"""
        self.executor.submit(self.run, document_id)

    def run(self, document_id: UUID) -> None:
        """以数据库互斥锁串行执行同一文档任务，失败保留终态而非永久处理中。"""
        from general_agent.knowledge.ingestion import ingest
        try:
            with self.database.connect() as lock:
                lock.autocommit = True
                lock.execute('SELECT pg_advisory_lock(%s,%s)', (0x4B4E4F57, document_id.int % (2**31 - 1)))
                row = self.current(document_id)
                if row is None:
                    return
                if row['status'] == 'deleting' or row['deleting']:
                    self.cleanup(document_id)
                elif row['status'] == 'processing':
                    ingest(self, row)
        except Exception:
            LOGGER.exception('知识库任务未完成 document_id=%s', document_id)
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE knowledge_documents SET status='failed',error='处理未完成，请重试。',updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=%s AND status='processing'", (document_id,),
                )

    def current(self, document_id: UUID) -> dict[str, Any] | None:
        """读取任务与所属知识库当前状态，处理路径每次发布前都重新核验。"""
        with self.database.connect() as connection:
            return connection.execute(
                'SELECT d.*,b.user_id,b.deleting FROM knowledge_documents d JOIN knowledge_bases b ON b.id=d.knowledge_base_id WHERE d.id=%s',
                (document_id,),
            ).fetchone()

    def progress(self, document_id: UUID, attempt: UUID, stage: str, completed: int = 0, total: int | None = None) -> None:
        """仅更新仍有效的当前处理尝试，删除或新尝试使迟到结果失效。"""
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE knowledge_documents d SET stage=%s,completed=%s,total=%s,updated_at=CURRENT_TIMESTAMP "
                "FROM knowledge_bases b WHERE d.id=%s AND d.attempt=%s AND d.status='processing' "
                "AND b.id=d.knowledge_base_id AND NOT b.deleting RETURNING d.id",
                (stage, completed, total, document_id, attempt),
            ).fetchone()
        if updated is None:
            raise LookupError('文档处理已撤销。')

    def retry(self, base_id: UUID, document_id: UUID, account_id: UUID) -> None:
        """只重试终态文档，复用已保存的成功图片、切片和当前版本向量。"""
        with self.database.connect() as connection:
            self.guard(connection, base_id, account_id, lock=True)
            row = connection.execute(
                "UPDATE knowledge_documents SET status='processing',stage='queued',attempt=%s,error=NULL,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=%s AND knowledge_base_id=%s AND status IN ('ready','failed') RETURNING id", (uuid4(), document_id, base_id),
            ).fetchone()
            if row is None:
                raise ValueError('文档不存在、正在处理或正在删除。')
        self.schedule(document_id)

    def file(self, base_id: UUID, document_id: UUID, account_id: UUID) -> tuple[Path, str]:
        """授权后返回仍存在的原件，删除中立即拒绝下载。"""
        with self.database.connect() as connection:
            self.guard(connection, base_id, account_id)
            row = connection.execute("SELECT filename FROM knowledge_documents WHERE id=%s AND knowledge_base_id=%s AND status<>'deleting'",
                                     (document_id, base_id)).fetchone()
        path = self.directory / str(document_id) / 'original'
        if row is None or not path.is_file():
            raise LookupError('知识库文档不存在。')
        return path, row['filename']

    def image_file(self, base_id: UUID, document_id: UUID, image_id: UUID, account_id: UUID) -> tuple[Path, str]:
        """授权后返回仍存在的知识库原图及其真实媒体类型。"""
        with self.database.connect() as connection:
            self.guard(connection, base_id, account_id)
            row = connection.execute(
                'SELECT i.metadata FROM knowledge_document_images i JOIN knowledge_documents d ON d.id=i.document_id '
                "WHERE i.id=%s AND i.document_id=%s AND d.knowledge_base_id=%s AND d.status='ready'",
                (image_id, document_id, base_id),
            ).fetchone()
        storage_name = str((row or {}).get('metadata', {}).get('storage_name', ''))
        path = self.directory / str(document_id) / 'images' / Path(storage_name).name
        if row is None or not path.is_file():
            raise LookupError('知识库图片不存在。')
        return path, str(row['metadata'].get('mime') or 'application/octet-stream')

    def delete(self, base_id: UUID, account_id: UUID, document_id: UUID | None = None) -> None:
        """立即撤销对象的读取和选择范围，随后后台执行可重试的文件清理。"""
        with self.database.connect() as connection:
            existing = connection.execute('SELECT id FROM knowledge_bases WHERE id=%s AND user_id=%s FOR UPDATE', (base_id, account_id)).fetchone()
            if existing is None:
                raise LookupError('知识库不存在。')
            if document_id is None:
                connection.execute('UPDATE knowledge_bases SET deleting=TRUE WHERE id=%s', (base_id,))
                connection.execute('DELETE FROM conversation_knowledge_bases WHERE knowledge_base_id=%s', (base_id,))
            rows = connection.execute(
                "UPDATE knowledge_documents SET status='deleting',updated_at=CURRENT_TIMESTAMP WHERE knowledge_base_id=%s "
                'AND (%s::uuid IS NULL OR id=%s) RETURNING id,operation_id', (base_id, document_id, document_id),
            ).fetchall()
            if document_id is not None and not rows:
                raise LookupError('知识库文档不存在。')
            for row in rows:
                if row['operation_id']:
                    connection.execute('UPDATE cloud_operations SET cancelled=TRUE WHERE id=%s', (row['operation_id'],))
        for row in rows:
            self.schedule(row['id'])
        self.cleanup_bases()

    def cleanup(self, document_id: UUID) -> None:
        """清理远端及本机文件后删除派生记录，失败保留 deleting 状态供后续重试。"""
        with self.database.connect() as connection:
            images = connection.execute('SELECT provider_file FROM knowledge_document_images WHERE document_id=%s', (document_id,)).fetchall()
        try:
            for image in images:
                if image['provider_file']:
                    self.files.delete(image['provider_file'])
            folder = self.directory / str(document_id)
            if folder.exists():
                shutil.rmtree(folder)
            with self.database.connect() as connection:
                connection.execute("DELETE FROM knowledge_documents WHERE id=%s AND status='deleting'", (document_id,))
        except (ValueError, OSError):
            with self.database.connect() as connection:
                connection.execute("UPDATE knowledge_documents SET error='清理未完成，请重试删除。' WHERE id=%s AND status='deleting'", (document_id,))
        self.cleanup_bases()

    def cleanup_bases(self) -> None:
        """仅删除没有剩余文档的已撤销知识库，外键自动移除失效选择。"""
        with self.database.connect() as connection:
            connection.execute('DELETE FROM knowledge_bases b WHERE deleting AND NOT EXISTS(SELECT 1 FROM knowledge_documents d WHERE d.knowledge_base_id=b.id)')
