"""管理会话知识库、图片文件身份及附件三十天生命周期。"""

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4

import httpx

from psycopg.types.json import Jsonb

from general_agent.attachments.images import validate_image
from general_agent.attachments.files import DeepSeekFiles, file_api_base
from general_agent.infrastructure.database import Database
from general_agent.attachments.documents import parse_document
from general_agent.attachments.knowledge import ChunkPolicy, store_units, text_units
from general_agent.attachments.pagination import paginate_word
from general_agent.attachments.tools import DocumentTools
from general_agent.attachments.retrieval import DocumentRetrieval
from general_agent.infrastructure.provider import FilePart, ModelMessage, TextPart
from general_agent.infrastructure.settings import SettingsStore
from general_agent.attachments.upload import UploadedFile
from general_agent.prompts import attachment_data, image_material_label

LOGGER = logging.getLogger(__name__)
FILE_LOCK_NAMESPACE = 0x46494C45


@dataclass
class AttachmentContext:
    """保存本轮文档清单和图片身份，不包含文字正文或动作授权。"""
    messages: list[ModelMessage] = field(default_factory=list)


class AttachmentService:
    """隐藏会话知识库、文件生命周期、固定分页和图片读取。"""

    def __init__(self, database: Database, directory: Path, transport: httpx.BaseTransport | None = None) -> None:
        """绑定文件服务、递归切片策略和复用嵌入配置的混合检索服务。"""
        self.database = database
        self.directory = directory / 'attachments'
        self.files = DeepSeekFiles(SettingsStore(directory), transport)
        self.policy = ChunkPolicy(int(os.environ.get('DOCUMENT_CHUNK_CHARACTERS', '1500')),
                                  int(os.environ.get('DOCUMENT_SEARCH_RESULTS', '5')),
                                  int(os.environ.get('DOCUMENT_CHUNK_OVERLAP', '200')))
        self.documents = DocumentTools(database, self.policy, DocumentRetrieval(database, directory, transport))

    def recover(self) -> None:
        """恢复中断处理并从原文件升级为递归重叠索引；不更改原有到期时间。"""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.database.connect() as connection:
            connection.execute("UPDATE attachments SET status='failed', error='应用重启中断了文件处理，请重新上传。' WHERE status='processing'")
        for path in self.directory.glob('*.upload'):
            path.unlink(missing_ok=True)
        for path in self.directory.glob('.pagination-*'):
            if path.is_dir():
                shutil.rmtree(path)
        self.expire()
        with self.database.connect() as connection:
            legacy = connection.execute(
                "UPDATE attachments SET status='processing' WHERE status='ready' AND image_metadata IS NULL "
                "AND knowledge_version<2 AND expires_at>CURRENT_TIMESTAMP RETURNING id",
            ).fetchall()
        for row in legacy:
            self.process(row['id'])

    def register(self, conversation_id: UUID, account_id: UUID, uploaded: list[UploadedFile]) -> list[dict[str, Any]]:
        """仅在仍有效会话原子登记上传，删除中拒绝登记，数据库失败清理本次原文件。"""
        stored: list[Path] = []
        rows: list[dict[str, Any]] = []
        try:
            with self.database.connect() as connection:
                if not connection.execute('SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting FOR KEY SHARE', (conversation_id, account_id)).fetchone():
                    raise LookupError('会话不存在。')
                for item in uploaded:
                    identifier = uuid4()
                    destination = self.directory / str(identifier)
                    item.path.replace(destination)
                    stored.append(destination)
                    row = connection.execute("INSERT INTO attachments (id,conversation_id,filename,size,status) VALUES (%s,%s,%s,%s,'processing') RETURNING *", (identifier, conversation_id, item.filename, item.size)).fetchone()
                    assert row is not None
                    rows.append(row)
        except BaseException:
            for path in stored:
                path.unlink(missing_ok=True)
            raise
        finally:
            for item in uploaded:
                item.path.unlink(missing_ok=True)
        return rows

    def bind_to_user_message(self, connection: Any, conversation_id: UUID, message_id: UUID) -> None:
        """把尚未展示过的会话附件关联到这条用户消息，只记录发送位置而不收窄回答上下文。"""
        connection.execute(
            "UPDATE attachments SET user_message_id=%s "
            "WHERE conversation_id=%s AND user_message_id IS NULL AND status<>'deleting'",
            (message_id, conversation_id),
        )

    def process(self, attachment_id: UUID) -> None:
        """图片上传与删除共用锁；文档解析保持原有非阻塞删除行为。"""
        with self.database.connect() as connection:
            row = connection.execute('SELECT filename FROM attachments WHERE id=%s', (attachment_id,)).fetchone()
        if row is None:
            return
        if Path(row['filename']).suffix.lower() not in {'.png', '.jpg', '.jpeg'}:
            self.process_file(attachment_id)
            return
        with self.database.connect() as lock:
            lock.autocommit = True
            lock.execute('SELECT pg_advisory_lock(%s,%s)', (FILE_LOCK_NAMESPACE, attachment_id.int % (2**31 - 1)))
            self.process_file(attachment_id)

    def process_file(self, attachment_id: UUID) -> None:
        """发布原文及第二版重叠索引或上传图片；重核来源和 TTL，阻止迟到发布。"""
        info: dict[str, Any] = {}
        content: str | None = None
        try:
            with self.database.connect() as connection:
                row = connection.execute("SELECT filename FROM attachments WHERE id=%s AND status='processing' AND expires_at>CURRENT_TIMESTAMP", (attachment_id,)).fetchone()
            if row is None:
                return
            path = self.directory / str(attachment_id)
            extension = Path(str(row['filename'])).suffix.lower()
            if extension in {'.png', '.jpg', '.jpeg'}:
                metadata = validate_image(path, extension)
                remote = self.files.upload(path, str(row['filename']), metadata['mime'])
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE attachments SET provider_file=%s,image_metadata=%s,"
                        "status=CASE WHEN status='processing' THEN 'ready' ELSE status END,error=NULL "
                        "WHERE id=%s AND (status IN ('processing','deleting') OR expires_at<=CURRENT_TIMESTAMP)",
                        (Jsonb(remote), Jsonb(metadata), attachment_id),
                    )
                return
            with TemporaryDirectory(dir=self.directory, prefix='.pagination-') as temporary:
                paginated = Path(temporary) / 'document.pdf'
                if extension in {'.pdf', '.docx'}:
                    document = parse_document(path, extension)
                    info = document.info.model_dump()
                    if document.error:
                        raise ValueError(document.error)
                    if extension == '.docx':
                        paginate_word(path, paginated)
                        document = parse_document(paginated, '.pdf')
                        document.info.format = 'docx'
                        document.info.limitations.append('Word 文字来自固定分页副本；修订及附属内容以该副本中的可见内容为准。')
                        info = document.info.model_dump()
                        if document.error:
                            raise ValueError(document.error)
                    units = document.units
                    content = self.document_content(document.chunks)
                else:
                    text = path.read_bytes().decode('utf-8-sig')
                    if not text.strip():
                        raise ValueError('文件没有可读取的文字。')
                    if any(ord(char) < 32 and char not in '\n\r\t' for char in text):
                        raise ValueError('文件包含二进制控制字符，不能作为文本读取。')
                    units = text_units(text)
                    content = text
                    info = {'format': extension.lstrip('.'), 'total_units': len(units),
                            'readable_units': sum(bool(unit.content.strip()) for unit in units)}
                with self.database.connect() as connection:
                    if not connection.execute("SELECT id FROM attachments WHERE id=%s AND status='processing' AND expires_at>CURRENT_TIMESTAMP FOR UPDATE", (attachment_id,)).fetchone():
                        return
                    store_units(connection, attachment_id, units, self.policy)
                    if paginated.exists():
                        paginated.replace(self.directory / f'{attachment_id}.pdf')
                    connection.execute("UPDATE attachments SET status='ready',error=NULL,parse_info=%s,content=%s,knowledge_version=2 WHERE id=%s", (Jsonb(info), content, attachment_id))
        except (UnicodeDecodeError, ValueError, OSError) as error:
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeDecodeError) else '无法读取文字；请上传 UTF-8 编码的 TXT 或 Markdown 文件。'
            with self.database.connect() as connection:
                connection.execute("UPDATE attachments SET status='failed', error=%s,parse_info=%s WHERE id=%s AND status='processing'", (message,Jsonb(info),attachment_id))
        except Exception:
            LOGGER.exception("附件处理失败 attachment_id=%s", attachment_id)
            with self.database.connect() as connection:
                connection.execute("UPDATE attachments SET status='failed', error='文件处理失败，请重新上传。' WHERE id=%s AND status='processing'", (attachment_id,))

    @staticmethod
    def document_content(chunks: list[tuple[str, str]]) -> str:
        """把解析器的页或段落片段合并成保留位置的完整文字。"""
        return '\n\n'.join(f'[{location}]\n{text}' for location, text in chunks)

    def list_attachments(self, conversation_id: UUID, account_id: UUID) -> list[dict[str, Any]]:
        """列出会话内未到期附件的持久状态，不返回正文或已过期资料。"""
        with self.database.connect() as connection:
            if not connection.execute('SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting', (conversation_id, account_id)).fetchone():
                raise LookupError('会话不存在。')
            return connection.execute('SELECT id,conversation_id,filename,size,status,error,created_at,expires_at,parse_info,image_metadata FROM attachments WHERE conversation_id=%s AND expires_at>CURRENT_TIMESTAMP ORDER BY created_at,id', (conversation_id,)).fetchall()

    def file(self, conversation_id: UUID, attachment_id: UUID, account_id: UUID) -> tuple[Path, str]:
        """仅允许下载所属未删除会话的未到期附件，磁盘路径由 UUID 决定。"""
        with self.database.connect() as connection:
            row = connection.execute("SELECT a.filename FROM attachments a JOIN conversations c ON c.id=a.conversation_id WHERE a.conversation_id=%s AND a.id=%s AND c.user_id=%s AND a.status<>'deleting' AND a.expires_at>CURRENT_TIMESTAMP AND NOT c.deleting", (conversation_id, attachment_id, account_id)).fetchone()
        path = self.directory / str(attachment_id)
        if row is None or not path.is_file():
            raise LookupError('当前会话中没有这个附件。')
        return path, str(row['filename'])

    def prepare(self, conversation_id: UUID, account_id: UUID) -> AttachmentContext:
        """每轮提供已升级的有效文档清单和图片身份，正文由工具按需读取。"""
        with self.database.connect() as connection:
            if not connection.execute(
                "SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting",
                (conversation_id, account_id),
            ).fetchone():
                raise LookupError("会话不存在。")
            rows = connection.execute(
                "SELECT id,filename,parse_info,image_metadata,provider_file,knowledge_version FROM attachments "
                "WHERE conversation_id=%s AND status='ready' AND expires_at>CURRENT_TIMESTAMP ORDER BY created_at,id",
                (conversation_id,),
            ).fetchall()

        documents = [
            {
                'document_id': str(row['id']),
                'filename': row['filename'],
                ('pages' if row['parse_info'].get('format') in ('pdf', 'docx') else 'paragraphs'): row['parse_info'].get('total_units', 0),
            }
            for row in rows
            if row['image_metadata'] is None and row['knowledge_version'] == 2
        ]
        images = [row for row in rows if row['image_metadata'] is not None]
        if not documents and not images:
            return AttachmentContext()

        messages: list[ModelMessage] = []
        if documents:
            messages.append({'role': 'user', 'content': attachment_data(documents)})
        if images:
            parts: list[TextPart | FilePart] = []
            for index, row in enumerate(images, 1):
                parts.append({'type': 'text', 'text': image_material_label(f'图片{index}', str(row['filename']))})
                remote = self.image_file(row['id'])
                parts.append({'type': 'file', 'file_id': remote['id']})
            messages.append({'role': 'user', 'content': parts})
        return AttachmentContext(messages)

    def image_file(self, attachment_id: UUID) -> dict[str, Any]:
        """读取未到期图片身份，旧图片补上传；同时核对应用及供应商有效期。"""
        with self.database.connect() as lock:
            lock.autocommit = True
            lock.execute('SELECT pg_advisory_lock(%s,%s)', (FILE_LOCK_NAMESPACE, attachment_id.int % (2**31 - 1)))
            row = lock.execute(
                "SELECT filename,image_metadata,provider_file FROM attachments WHERE id=%s AND status='ready' AND expires_at>CURRENT_TIMESTAMP",
                (attachment_id,),
            ).fetchone()
            if row is None:
                raise ValueError('图片已删除或不可用，请重新上传。')
            remote = row['provider_file']
            if remote is None:
                remote = self.files.upload(self.directory / str(attachment_id), row['filename'], row['image_metadata']['mime'])
                lock.execute("UPDATE attachments SET provider_file=%s WHERE id=%s AND status IN ('ready','deleting')", (Jsonb(remote), attachment_id))
            if remote['expires_at'] <= time.time():
                lock.execute("UPDATE attachments SET status='failed',error='图片的 30 天有效期已结束，请重新上传。' WHERE id=%s AND status='ready'", (attachment_id,))
                raise ValueError('图片的 30 天有效期已结束，请重新上传。')
            settings = self.files.settings.read()
            if settings is None or file_api_base(settings) != remote['base_url']:
                raise ValueError('LLM 服务地址已变化，请在当前服务重新上传图片。')
            if not lock.execute("SELECT id FROM attachments WHERE id=%s AND status='ready' AND expires_at>CURRENT_TIMESTAMP", (attachment_id,)).fetchone():
                raise ValueError('图片已删除或不可用。')
            return dict(remote)

    def delete_file(self, attachment_id: UUID) -> None:
        """与图片上传串行清理远端、原文件和分页副本；失败保留身份重试。"""
        with self.database.connect() as lock:
            lock.autocommit = True
            lock.execute('SELECT pg_advisory_lock(%s,%s)', (FILE_LOCK_NAMESPACE, attachment_id.int % (2**31 - 1)))
            row = lock.execute('SELECT provider_file FROM attachments WHERE id=%s', (attachment_id,)).fetchone()
            if row and row['provider_file']:
                self.files.delete(row['provider_file'])
                lock.execute('UPDATE attachments SET provider_file=NULL WHERE id=%s', (attachment_id,))
            (self.directory / str(attachment_id)).unlink(missing_ok=True)
            (self.directory / f'{attachment_id}.pdf').unlink(missing_ok=True)

    def expire(self) -> None:
        """先停用到期附件，再清理所有派生内容和文件；失败保留到期身份供定时重试。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "UPDATE attachments SET status='failed',error='附件的 30 天有效期已结束，请重新上传。' "
                "WHERE expires_at<=CURRENT_TIMESTAMP AND NOT expired_cleaned AND status<>'deleting' RETURNING id",
            ).fetchall()
        for row in rows:
            try:
                self.delete_file(row['id'])
                with self.database.connect() as connection:
                    connection.execute('DELETE FROM document_units WHERE attachment_id=%s', (row['id'],))
                    connection.execute("UPDATE attachments SET content=NULL,expired_cleaned=TRUE WHERE id=%s AND expires_at<=CURRENT_TIMESTAMP", (row['id'],))
            except Exception:
                LOGGER.exception('到期附件清理未完成 attachment_id=%s', row['id'])
