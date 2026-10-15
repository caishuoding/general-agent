"""限额解析 multipart 文件流，拒绝截断并清理所有临时文件。"""

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from fastapi import HTTPException, Request
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

MAX_FILE_BYTES = 20_000_000
MAX_FILES = 5


@dataclass
class UploadedFile:
    """只在一次完整上传内使用的本机暂存文件。"""
    path: Path
    filename: str
    size: int = 0


class FileReceiver:
    """封装解析器回调，使文件字节、头部、数量和收尾均可验证。"""

    def __init__(self, directory: Path) -> None:
        """准备本次上传的句柄清单，不使用用户文件名拼接路径。"""
        self.directory = directory
        self.files: list[UploadedFile] = []
        self.handles: list[BinaryIO] = []
        self.headers: dict[bytes, bytes] = {}
        self.field = bytearray()
        self.value = bytearray()
        self.header_bytes = 0
        self.current: UploadedFile | None = None
        self.handle: BinaryIO | None = None
        self.ended = False

    def part_begin(self) -> None:
        """开始一个新 part，清空头部而保留全部已创建句柄供异常清理。"""
        if len(self.files) >= MAX_FILES:
            raise HTTPException(422, '每次上传及每条消息最多五个附件。')
        self.headers = {}
        self.field.clear()
        self.value.clear()
        self.header_bytes = 0
        self.current = None
        self.handle = None

    def header_field(self, data: bytes, start: int, end: int) -> None:
        """限额读取头字段，避免巨型文件名或恶意头部占满内存。"""
        self.header_bytes += end - start
        if self.header_bytes > 16_384:
            raise HTTPException(422, '附件头部过长。')
        self.field.extend(data[start:end])

    def header_value(self, data: bytes, start: int, end: int) -> None:
        """将头值长度计入同一硬上限。"""
        self.header_bytes += end - start
        if self.header_bytes > 16_384:
            raise HTTPException(422, '附件头部过长。')
        self.value.extend(data[start:end])

    def header_end(self) -> None:
        """保存完整头字段并拒绝重复头。"""
        key = bytes(self.field).lower()
        if key in self.headers:
            raise HTTPException(422, '附件头部重复。')
        self.headers[key] = bytes(self.value)
        self.field.clear()
        self.value.clear()

    def headers_finished(self) -> None:
        """验证文件字段后以随机路径创建私有暂存文件。"""
        _, options = parse_options_header(self.headers.get(b'content-disposition', b''))
        if options.get(b'name') != b'files' or b'filename' not in options:
            raise HTTPException(422, '请通过 files 字段上传附件，不接受普通表单字段。')
        try:
            filename = options[b'filename'].decode('utf-8')
        except UnicodeDecodeError:
            raise HTTPException(422, '附件文件名需要使用 UTF-8 编码。') from None
        filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
        if not filename or len(filename) > 240 or any(ord(char) < 32 for char in filename):
            raise HTTPException(422, '附件文件名无效或过长。')
        if Path(filename).suffix.lower() not in {'.txt', '.md', '.markdown', '.pdf', '.docx'}:
            raise HTTPException(422, '当前支持 TXT、Markdown、文本 PDF、DOCX；其他格式暂未启用。')
        path = self.directory / (uuid4().hex + '.upload')
        self.handle = path.open('xb')
        path.chmod(0o600)
        self.handles.append(self.handle)
        self.current = UploadedFile(path, filename)
        self.files.append(self.current)

    def part_data(self, data: bytes, start: int, end: int) -> None:
        """按实际文件字节在写盘前执行 20 MB 上限。"""
        if self.current is None or self.handle is None:
            raise HTTPException(422, '附件正文缺少有效文件头。')
        self.current.size += end - start
        if self.current.size > MAX_FILE_BYTES:
            raise HTTPException(413, '单个附件最多 20 MB（20,000,000 字节）。')
        self.handle.write(data[start:end])

    def part_end(self) -> None:
        """关闭已经完整收取的文件 part。"""
        if self.handle is None:
            raise HTTPException(422, '附件正文不完整。')
        self.handle.close()
        self.handle = None

    def end(self) -> None:
        """只有实际终止边界出现才认定整个上传完成。"""
        self.ended = True

    def cleanup(self, remove: bool) -> None:
        """关闭完整或未完成的所有句柄，失败时删除全部暂存字节。"""
        for handle in self.handles:
            handle.close()
        if remove:
            for uploaded in self.files:
                uploaded.path.unlink(missing_ok=True)


async def receive_files(request: Request, directory: Path) -> list[UploadedFile]:
    """流式接收最多五个文件，所有拒绝和断流均清理本次暂存资源。"""
    content_type, options = parse_options_header(request.headers.get('content-type', ''))
    boundary = options.get(b'boundary', b'')
    if content_type != b'multipart/form-data' or not boundary or len(boundary) > 200:
        raise HTTPException(422, '请使用完整的 multipart 附件上传请求。')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    receiver = FileReceiver(directory)
    parser = MultipartParser(boundary, {
        'on_part_begin': receiver.part_begin, 'on_header_field': receiver.header_field,
        'on_header_value': receiver.header_value, 'on_header_end': receiver.header_end,
        'on_headers_finished': receiver.headers_finished, 'on_part_data': receiver.part_data,
        'on_part_end': receiver.part_end, 'on_end': receiver.end,
    })
    success = False
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_FILES * MAX_FILE_BYTES + 131_072:
                raise HTTPException(413, '附件上传请求超过总大小限制。')
            for offset in range(0, len(chunk), 65_536):
                parser.write(chunk[offset:offset + 65_536])
        parser.finalize()
        if not receiver.ended or receiver.handle is not None or not receiver.files:
            raise HTTPException(422, '附件上传未完整结束，请重新上传。')
        success = True
        return receiver.files
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, '附件上传失败或正文损坏，请重新上传。') from None
    finally:
        receiver.cleanup(remove=not success)
