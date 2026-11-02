"""通过目录描述符操作不可信工作文件，避免检查路径后的符号链接竞态。"""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO


class SafeFiles:
    """所有路径逐级使用 O_NOFOLLOW 打开，打开后的描述符不受目录替换影响。"""

    def __init__(self, root: Path) -> None:
        """根目录必须由后端创建，其父目录不得暴露给任务容器。"""
        self.root = root

    @staticmethod
    def parts(relative: str) -> tuple[str, ...]:
        """校验相对路径，不允许上级目录、绝对路径和内部命令记录。"""
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or '\x00' in relative or any(part.startswith('.runtime') for part in path.parts):
            raise ValueError('工作文件路径无效或越界。')
        return path.parts

    @contextmanager
    def directory(self, parts: tuple[str, ...], create: bool = False) -> Iterator[int]:
        """逐级打开真实目录；写入时仅相对已打开目录创建缺失层级。"""
        descriptors: list[int] = []
        try:
            descriptors.append(os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
                    except FileExistsError:
                        pass
                descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
            yield descriptors[-1]
        except OSError:
            raise ValueError('工作文件路径不存在、包含链接或不可访问。') from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @contextmanager
    def file(self, relative: str, write: bool = False) -> Iterator[BinaryIO]:
        """打开普通文件且不跟随链接；拒绝设备、套接字、FIFO 和目录。"""
        parts = self.parts(relative)
        if not parts:
            raise ValueError('必须指定普通工作文件路径。')
        with self.directory(parts[:-1], create=write) as directory:
            flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_WRONLY | os.O_CREAT | os.O_TRUNC if write else os.O_RDONLY)
            descriptor = os.open(parts[-1], flags, mode=0o600, dir_fd=directory)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise ValueError('只允许读写普通工作文件。')
            with os.fdopen(descriptor, 'wb' if write else 'rb') as stream:
                yield stream

    def read(self, relative: str, offset: int, limit: int) -> dict[str, Any]:
        """返回目录清单或有界文本，所有检查和读取均使用同一描述符。"""
        parts = self.parts(relative)
        with self.directory(parts[:-1]) as directory:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory) if parts else os.dup(directory)
            try:
                info = os.fstat(descriptor)
                if stat.S_ISDIR(info.st_mode):
                    entries = sorted(os.listdir(descriptor))
                    return {'entries': entries[:500], 'has_more': len(entries) > 500}
                if not stat.S_ISREG(info.st_mode) or info.st_size > 5_000_000:
                    raise ValueError('只允许读取不超过五百万字节的普通文本文件。')
                with os.fdopen(os.dup(descriptor), 'rb') as stream:
                    data = stream.read(5_000_001)
                if len(data) > 5_000_000:
                    raise ValueError('读取中的文本文件超过大小限制。')
                text = data.decode('utf-8')
                end = offset + limit
                return {'content': text[offset:end], 'total_characters': len(text), 'next_offset': end if end < len(text) else None}
            finally:
                os.close(descriptor)
