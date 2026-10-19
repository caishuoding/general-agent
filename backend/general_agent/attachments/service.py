"""独立于长期记忆管理会话附件、完整可读文字和图片原文件。"""

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.attachments.images import validate_image
from general_agent.infrastructure.database import Database
from general_agent.attachments.documents import parse_document
from general_agent.infrastructure.provider import ImagePart, ModelMessage, TextPart
from general_agent.attachments.upload import UploadedFile
from general_agent.prompts import attachment_data, image_material_label


@dataclass
class AttachmentContext:
    """保存本次实际提供的完整附件材料，不携带动作授权。"""
    messages: list[ModelMessage] = field(default_factory=list)


class AttachmentService:
    """隐藏本机文件生命周期、会话过滤、全文解析和图片读取。"""

    def __init__(self, database: Database, directory: Path) -> None:
        """绑定业务数据库及私有原文件目录，原始文件名仅用于显示。"""
        self.database = database
        self.directory = directory / 'attachments'

    def recover(self) -> None:
        """重启将未完成解析标成失败，清理尚未登记的暂存文件。"""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.database.connect() as connection:
            connection.execute("UPDATE attachments SET status='failed', error='应用重启中断了文件处理，请重新上传。' WHERE status='processing'")
        for path in self.directory.glob('*.upload'):
            path.unlink(missing_ok=True)

    def register(self, conversation_id: UUID, uploaded: list[UploadedFile]) -> list[dict[str, Any]]:
        """仅在仍有效会话原子登记上传，删除中拒绝登记，数据库失败清理本次原文件。"""
        stored: list[Path] = []
        rows: list[dict[str, Any]] = []
        try:
            with self.database.connect() as connection:
                if not connection.execute('SELECT id FROM conversations WHERE id=%s AND NOT deleting FOR KEY SHARE', (conversation_id,)).fetchone():
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
        """解析全文后在写入事务重核 processing 身份，删除中及晚到结果不能恢复文件状态。"""
        info: dict[str, Any] = {}
        content: str | None = None
        try:
            with self.database.connect() as connection:
                row = connection.execute("SELECT filename FROM attachments WHERE id=%s AND status='processing'", (attachment_id,)).fetchone()
            if row is None:
                return
            path = self.directory / str(attachment_id)
            extension = Path(str(row['filename'])).suffix.lower()
            if extension in {'.png', '.jpg', '.jpeg'}:
                metadata = validate_image(path, extension)
                with self.database.connect() as connection:
                    if not connection.execute("SELECT id FROM attachments WHERE id=%s AND status='processing' FOR UPDATE",(attachment_id,)).fetchone():
                        return
                    connection.execute("UPDATE attachments SET status='ready',image_metadata=%s,error=NULL WHERE id=%s AND status='processing'", (Jsonb(metadata), attachment_id))
                return
            elif extension in {'.pdf', '.docx'}:
                document = parse_document(path, extension)
                info = document.info.model_dump()
                if document.error:
                    raise ValueError(document.error)
                content = self.document_content(document.chunks)
            else:
                text = path.read_bytes().decode('utf-8-sig')
                if not text.strip():
                    raise ValueError('文件没有可读取的文字。')
                if any(ord(char) < 32 and char not in '\n\r\t' for char in text):
                    raise ValueError('文件包含二进制控制字符，不能作为文本读取。')
                content = text
            with self.database.connect() as connection:
                if not connection.execute("SELECT id FROM attachments WHERE id=%s AND status='processing' FOR UPDATE",(attachment_id,)).fetchone():
                    return
                connection.execute("UPDATE attachments SET status='ready', error=NULL,parse_info=%s,content=%s WHERE id=%s AND status='processing'", (Jsonb(info),content,attachment_id))
        except (UnicodeDecodeError, ValueError, OSError) as error:
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeDecodeError) else '无法读取文字；请上传 UTF-8 编码的 TXT 或 Markdown 文件。'
            with self.database.connect() as connection:
                connection.execute("UPDATE attachments SET status='failed', error=%s,parse_info=%s WHERE id=%s AND status='processing'", (message,Jsonb(info),attachment_id))
        except Exception:
            with self.database.connect() as connection:
                connection.execute("UPDATE attachments SET status='failed', error='文件处理失败，请重新上传。' WHERE id=%s AND status='processing'", (attachment_id,))

    @staticmethod
    def document_content(chunks: list[tuple[str, str]]) -> str:
        """把解析器的页或段落片段合并成保留位置的完整文字。"""
        return '\n\n'.join(f'[{location}]\n{text}' for location, text in chunks)

    def list_attachments(self, conversation_id: UUID) -> list[dict[str, Any]]:
        """列出仍有效会话附件的持久状态，删除中不能重新获取资料。"""
        with self.database.connect() as connection:
            if not connection.execute('SELECT id FROM conversations WHERE id=%s AND NOT deleting', (conversation_id,)).fetchone():
                raise LookupError('会话不存在。')
            return connection.execute('SELECT id,conversation_id,filename,size,status,error,created_at,parse_info,image_metadata FROM attachments WHERE conversation_id=%s ORDER BY created_at,id', (conversation_id,)).fetchall()

    def file(self, conversation_id: UUID, attachment_id: UUID) -> tuple[Path, str]:
        """按仍有效会话与附件授权下载；删除中即使文件尚在也不能读取，路径仅由 UUID 决定。"""
        with self.database.connect() as connection:
            row = connection.execute("SELECT a.filename FROM attachments a JOIN conversations c ON c.id=a.conversation_id WHERE a.conversation_id=%s AND a.id=%s AND a.status<>'deleting' AND NOT c.deleting", (conversation_id, attachment_id)).fetchone()
        path = self.directory / str(attachment_id)
        if row is None or not path.is_file():
            raise LookupError('当前会话中没有这个附件。')
        return path, str(row['filename'])

    def prepare(self, conversation_id: UUID) -> AttachmentContext:
        """读取当前会话全部可用附件，并把完整文字或图片原字节交给模型。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,filename,content,parse_info,image_metadata FROM attachments "
                "WHERE conversation_id=%s AND status='ready' ORDER BY created_at,id",
                (conversation_id,),
            ).fetchall()

        documents = [
            {
                'attachment_id': str(row['id']),
                'filename': row['filename'],
                'content': row['content'],
                'parse_info': row['parse_info'],
            }
            for row in rows
            if row['image_metadata'] is None and row['content']
        ]
        images = [row for row in rows if row['image_metadata'] is not None]
        if not documents and not images:
            return AttachmentContext()

        messages: list[ModelMessage] = []
        if documents:
            messages.append({'role': 'user', 'content': attachment_data(documents)})
        if images:
            parts: list[TextPart | ImagePart] = []
            for index, row in enumerate(images, 1):
                parts.append({'type': 'text', 'text': image_material_label(f'图片{index}', str(row['filename']))})
                encoded = base64.b64encode((self.directory / str(row['id'])).read_bytes()).decode('ascii')
                parts.append({'type': 'image_url', 'image_url': {'url': f'data:{row["image_metadata"]["mime"]};base64,{encoded}'}})
            messages.append({'role': 'user', 'content': parts})
        return AttachmentContext(messages)
