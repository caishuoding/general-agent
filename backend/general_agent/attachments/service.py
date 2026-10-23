"""管理会话知识库、图片文件身份及附件三十天生命周期。"""

import logging
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
from general_agent.attachments.pdf_text import PDF_TEXT_SOURCE
from general_agent.attachments.document_images import DocumentVision, ParsedImage, VISION_PROMPT_VERSION
from general_agent.attachments.knowledge import (
    ChunkPolicy,
    configured_chunk_policy,
    store_chunks,
    store_document_images,
    store_image_chunks,
    text_units,
)
from general_agent.attachments.pagination import paginate_word
from general_agent.attachments.tools import DocumentTools
from general_agent.attachments.retrieval import DocumentRetrieval
from general_agent.infrastructure.provider import FilePart, ModelFailure, ModelMessage, TextPart
from general_agent.infrastructure.settings import SettingsStore
from general_agent.attachments.upload import UploadedFile
from general_agent.prompts import attachment_data, image_material_label

LOGGER = logging.getLogger(__name__)
FILE_LOCK_NAMESPACE = 0x46494C45


def tool_result_image_ids(results: list[dict[str, Any]]) -> list[UUID]:
    """从搜索或读取工具结果中提取稳定图片身份，并保持首次出现顺序。"""
    identifiers: list[UUID] = []

    def collect(value: Any) -> None:
        """递归扫描工具结果中的 image_id 字段，不解释其他外部内容。"""
        if isinstance(value, dict):
            candidate = value.get('image_id')
            if isinstance(candidate, str):
                try:
                    identifiers.append(UUID(candidate))
                except ValueError:
                    pass
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(results)
    return list(dict.fromkeys(identifiers))


@dataclass
class AttachmentContext:
    """保存本轮文档清单和图片身份，不包含文字正文或动作授权。"""
    messages: list[ModelMessage] = field(default_factory=list)


class AttachmentService:
    """隐藏会话知识库、文件生命周期、固定分页和图片读取。"""

    def __init__(self, database: Database, directory: Path, transport: httpx.BaseTransport | None = None) -> None:
        """绑定文档处理依赖；每个新任务按当前回答 LLM 获取切片计数快照。"""
        self.database = database
        self.settings_directory = directory
        self.directory = directory / 'attachments'
        self.files = DeepSeekFiles(SettingsStore(directory), transport)
        self.vision = DocumentVision(directory, self.files, transport)
        self.documents = DocumentTools(database, self.policy, DocumentRetrieval(database, directory, transport))

    @property
    def policy(self) -> ChunkPolicy:
        """返回最新 LLM 配置对应的策略，新任务不沿用服务启动时的模型选择。"""
        return configured_chunk_policy(self.settings_directory)

    def recover(self) -> None:
        """按结构和解析版本恢复旧索引，保留人工清空状态，不因单独切换 LLM 重切。"""
        policy = self.policy
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
                "AND COALESCE(parse_info->>'index_cleared','false')<>'true' "
                "AND (knowledge_version<2 OR (lower(filename) LIKE '%%.docx' "
                "AND (parse_info->>'text_source' IS DISTINCT FROM 'word_native_markdown_v3' "
                "OR COALESCE(parse_info->>'chunk_layout',parse_info->>'chunk_policy') IS DISTINCT FROM %s "
                "OR (COALESCE((parse_info->>'image_count')::int,0)>0 "
                "AND parse_info->>'vision_rules' IS DISTINCT FROM %s))) "
                "OR (lower(filename) LIKE '%%.pdf' AND (parse_info->>'text_source' IS DISTINCT FROM %s "
                "OR COALESCE(parse_info->>'chunk_layout',parse_info->>'chunk_policy') IS DISTINCT FROM %s "
                "OR (COALESCE((parse_info->>'image_count')::int,0)>0 "
                "AND parse_info->>'vision_rules' IS DISTINCT FROM %s)))) "
                "AND expires_at>CURRENT_TIMESTAMP RETURNING id",
                (policy.layout_signature, VISION_PROMPT_VERSION, PDF_TEXT_SOURCE, policy.layout_signature, VISION_PROMPT_VERSION),
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
        """用同一 LLM 计数快照提取并切片，发布前重核来源与原 TTL。"""
        info: dict[str, Any] = {}
        try:
            with self.database.connect() as connection:
                row = connection.execute("SELECT filename FROM attachments WHERE id=%s AND status='processing' AND expires_at>CURRENT_TIMESTAMP", (attachment_id,)).fetchone()
            if row is None:
                return
            policy = self.policy
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
                image_directory = self.directory / f'{attachment_id}.images'
                if extension in {'.pdf', '.docx'}:
                    shutil.rmtree(image_directory, ignore_errors=True)
                    document = parse_document(path, extension, image_directory)
                    info = document.info.model_dump()
                    if document.error:
                        raise ValueError(document.error)
                    if extension == '.docx':
                        pages, positions, image_positions = paginate_word(path, paginated)
                        if len(positions) != len(document.units):
                            raise ValueError('Word 原文结构与分页定位数量不一致。')
                        for ordinal, unit in enumerate(document.units, 1):
                            unit.page, unit.end_page = positions[ordinal]
                        for image in document.images:
                            image.page = image_positions.get(image.ordinal)
                            image.end_page = image.page
                        document.info.total_units = pages
                        document.info.readable_units = pages
                        document.info.limitations.append('正文直接读取 Word 原件；固定分页副本仅提供页码，跨页段落和表格行保持完整。')
                        info = document.info.model_dump()
                    images = self.describe_images(image_directory, document.images)
                    described = sum(item['vision_status'] == 'complete' for item in images)
                    failed = sum(item['vision_status'] == 'failed' for item in images)
                    info.update(
                        image_count=len(images),
                        image_described_count=described,
                        image_failed_count=failed,
                        vision_model=self.vision.name,
                        vision_revision=self.vision.revision,
                        vision_rules=VISION_PROMPT_VERSION,
                    )
                    if images and not described and not any(unit.content.strip() for unit in document.units):
                        raise ValueError('文档图片未能生成描述，请检查视觉模型后重新处理。')
                    units = document.units
                else:
                    images = []
                    text = path.read_bytes().decode('utf-8-sig')
                    if not text.strip():
                        raise ValueError('文件没有可读取的文字。')
                    if any(ord(char) < 32 and char not in '\n\r\t' for char in text):
                        raise ValueError('文件包含二进制控制字符，不能作为文本读取。')
                    units = text_units(text)
                    info = {'format': extension.lstrip('.'), 'total_units': len(units),
                            'readable_units': sum(bool(unit.content.strip()) for unit in units)}
                info.update(chunk_policy=policy.signature, chunk_layout=policy.layout_signature,
                            chunk_tokens=policy.tokens, chunk_max_tokens=policy.max_tokens,
                            chunk_overlap_tokens=policy.overlap, token_count_method=policy.counter.method,
                            tokenizer_model=policy.counter.model, tokenizer_fingerprint=policy.counter.fingerprint)
                with self.database.connect() as connection:
                    if not connection.execute("SELECT id FROM attachments WHERE id=%s AND status='processing' AND expires_at>CURRENT_TIMESTAMP FOR UPDATE", (attachment_id,)).fetchone():
                        return
                    store_chunks(connection, attachment_id, units, policy, images=images)
                    if extension in {'.pdf', '.docx'}:
                        store_document_images(connection, attachment_id, images)
                        store_image_chunks(connection, attachment_id, images, policy)
                    if paginated.exists():
                        paginated.replace(self.directory / f'{attachment_id}.pdf')
                    connection.execute("UPDATE attachments SET status='ready',error=NULL,parse_info=%s,knowledge_version=2 WHERE id=%s", (Jsonb(info), attachment_id))
        except (UnicodeDecodeError, ValueError, OSError) as error:
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeDecodeError) else '无法读取文字；请上传 UTF-8 编码的 TXT 或 Markdown 文件。'
            self.remove_image_directory(attachment_id)
            with self.database.connect() as connection:
                connection.execute("UPDATE attachments SET status='failed', error=%s,parse_info=%s WHERE id=%s AND status='processing'", (message,Jsonb(info),attachment_id))
        except Exception:
            LOGGER.exception("附件处理失败 attachment_id=%s", attachment_id)
            self.remove_image_directory(attachment_id)
            with self.database.connect() as connection:
                connection.execute("UPDATE attachments SET status='failed', error='文件处理失败，请重新上传。' WHERE id=%s AND status='processing'", (attachment_id,))

    def describe_images(self, directory: Path, images: list[ParsedImage]) -> list[dict[str, Any]]:
        """按图片哈希复用纯文字描述，保留成功结果或单图失败原因。"""
        results: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any]] = {}
        for image in images:
            item = image.model_dump()
            if image.media_type == 'table_preview':
                item.update(description='表格原区域预览', vision_status='complete', vision_model='table-preview',
                            vision_revision='table-preview-v1', vision_error=None)
                results.append(item)
                continue
            if image.sha256 in cache:
                item.update(cache[image.sha256])
                results.append(item)
                continue
            try:
                description, remote = self.vision.describe(directory / image.storage_name, image)
                item.update(
                    description=description,
                    provider_file=remote,
                    vision_status='complete',
                    vision_model=self.vision.name,
                    vision_revision=self.vision.revision,
                )
            except (ValueError, ModelFailure, OSError, httpx.HTTPError) as error:
                item.update(
                    vision_status='failed',
                    vision_model=self.vision.name,
                    vision_revision=self.vision.revision,
                    vision_error=str(error),
                )
            cache[image.sha256] = {
                key: item[key] for key in (
                    'description', 'provider_file',
                    'vision_status', 'vision_model', 'vision_revision', 'vision_error',
                ) if key in item
            }
            results.append(item)
        return results

    def remove_image_directory(self, attachment_id: UUID) -> None:
        """在没有发布图片切片时删除本次派生的图片目录，避免留下孤立原图。"""
        shutil.rmtree(self.directory / f'{attachment_id}.images', ignore_errors=True)

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
        """串行清理独立图片、文档图片的远端身份，以及原件、分页副本和派生图片目录。"""
        with self.database.connect() as lock:
            lock.autocommit = True
            lock.execute('SELECT pg_advisory_lock(%s,%s)', (FILE_LOCK_NAMESPACE, attachment_id.int % (2**31 - 1)))
            row = lock.execute('SELECT provider_file FROM attachments WHERE id=%s', (attachment_id,)).fetchone()
            if row and row['provider_file']:
                self.files.delete(row['provider_file'])
                lock.execute('UPDATE attachments SET provider_file=NULL WHERE id=%s', (attachment_id,))
            document_files = lock.execute(
                'SELECT DISTINCT provider_file FROM document_images '
                'WHERE attachment_id=%s AND provider_file IS NOT NULL',
                (attachment_id,),
            ).fetchall()
            for item in document_files:
                self.files.delete(item['provider_file'])
            lock.execute('UPDATE document_images SET provider_file=NULL WHERE attachment_id=%s', (attachment_id,))
            (self.directory / str(attachment_id)).unlink(missing_ok=True)
            (self.directory / f'{attachment_id}.pdf').unlink(missing_ok=True)
            shutil.rmtree(self.directory / f'{attachment_id}.images', ignore_errors=True)

    def document_image_message(
        self,
        conversation_id: UUID,
        account_id: UUID,
        results: list[dict[str, Any]],
        excluded: set[UUID],
    ) -> tuple[ModelMessage | None, list[UUID]]:
        """把工具命中的原图身份加入下一轮模型请求，每轮同一图片只发送一次。"""
        identifiers = [
            item for item in tool_result_image_ids(results)
            if item not in excluded
        ][:5]
        if not identifiers:
            return None, []
        with self.database.connect() as connection:
            if not connection.execute(
                'SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting',
                (conversation_id, account_id),
            ).fetchone():
                raise LookupError('会话不存在。')
            rows = connection.execute(
                'SELECT i.id,i.attachment_id,i.storage_name,i.mime,i.provider_file,i.location,'
                'a.filename FROM document_images i JOIN attachments a ON a.id=i.attachment_id '
                'WHERE i.id=ANY(%s) AND a.conversation_id=%s AND a.status=\'ready\' '
                'AND a.expires_at>CURRENT_TIMESTAMP ORDER BY a.created_at,a.id,i.ordinal',
                (identifiers, conversation_id),
            ).fetchall()
        parts: list[TextPart | FilePart] = []
        selected: list[UUID] = []
        for index, row in enumerate(rows, 1):
            remote = self.document_image_file(row)
            label = f"{row['filename']}；{row['location']}"
            parts.append({'type': 'text', 'text': image_material_label(f'文档图片{index}', label)})
            parts.append({'type': 'file', 'file_id': remote['id']})
            selected.append(row['id'])
        return ({'role': 'user', 'content': parts} if parts else None), selected

    def document_image_file(self, row: dict[str, Any]) -> dict[str, Any]:
        """复用有效派生图片身份；过期或服务变更时从本地图片重新上传。"""
        remote = row['provider_file']
        settings = self.files.settings.read()
        if remote is not None and settings is not None and file_api_base(settings) == remote['base_url'] and remote['expires_at'] > time.time():
            return dict(remote)
        path = self.directory / f"{row['attachment_id']}.images" / row['storage_name']
        if not path.is_file():
            raise ValueError('文档图片原文件已不可用，请重新处理附件。')
        remote = self.files.upload(path, row['storage_name'], row['mime'])
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE document_images SET provider_file=%s WHERE id=%s AND ("
                "SELECT status='ready' AND expires_at>CURRENT_TIMESTAMP FROM attachments "
                "WHERE id=document_images.attachment_id)",
                (Jsonb(remote), row['id']),
            )
        return remote

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
                    connection.execute('DELETE FROM document_chunks WHERE attachment_id=%s', (row['id'],))
                    connection.execute('DELETE FROM document_images WHERE attachment_id=%s', (row['id'],))
                    connection.execute("UPDATE attachments SET expired_cleaned=TRUE WHERE id=%s AND expires_at<=CURRENT_TIMESTAMP", (row['id'],))
            except Exception:
                LOGGER.exception('到期附件清理未完成 attachment_id=%s', row['id'])
