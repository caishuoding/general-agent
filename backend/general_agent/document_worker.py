"""在独立且受资源限额保护的进程中解析不可信文档。"""

import ctypes
import logging
import sys
from ctypes import Structure
from ctypes import wintypes
from pathlib import Path
from typing import cast

from general_agent.document_limits import (
    MAX_DOCUMENT_CPU_SECONDS,
    MAX_DOCUMENT_OUTPUT_BYTES,
    MAX_DOCUMENT_RSS_BYTES,
)

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100


class _BasicLimitInformation(Structure):
    """描述 Windows Job Object 的每进程 CPU 限制。"""

    _fields_ = [
        ('per_process_user_time_limit', ctypes.c_longlong),
        ('per_job_user_time_limit', ctypes.c_longlong),
        ('limit_flags', wintypes.DWORD),
        ('minimum_working_set_size', ctypes.c_size_t),
        ('maximum_working_set_size', ctypes.c_size_t),
        ('active_process_limit', wintypes.DWORD),
        ('affinity', ctypes.c_size_t),
        ('priority_class', wintypes.DWORD),
        ('scheduling_class', wintypes.DWORD),
    ]


class _IoCounters(Structure):
    """补齐 Windows 扩展 Job Object 限制结构中的 I/O 计数。"""

    _fields_ = [
        ('read_operation_count', ctypes.c_ulonglong), ('write_operation_count', ctypes.c_ulonglong),
        ('other_operation_count', ctypes.c_ulonglong), ('read_transfer_count', ctypes.c_ulonglong),
        ('write_transfer_count', ctypes.c_ulonglong), ('other_transfer_count', ctypes.c_ulonglong),
    ]


class _ExtendedLimitInformation(Structure):
    """组合 Windows Job Object 的 CPU、内存及峰值字段。"""

    _fields_ = [
        ('basic_limit_information', _BasicLimitInformation), ('io_info', _IoCounters),
        ('process_memory_limit', ctypes.c_size_t), ('job_memory_limit', ctypes.c_size_t),
        ('peak_process_memory_used', ctypes.c_size_t), ('peak_job_memory_used', ctypes.c_size_t),
    ]


def _apply_windows_limits() -> object | None:
    """用 Job Object 对当前 Windows 工作进程执行 CPU 与内存硬限制。"""
    if sys.platform != 'win32':
        return None
    loader = getattr(ctypes, 'WinDLL', None)
    if loader is None:
        raise RuntimeError('当前 Windows 环境不能创建文档资源限制。')
    kernel32 = loader('kernel32', use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise RuntimeError('无法创建文档资源限制。')
    limits = _ExtendedLimitInformation()
    limits.basic_limit_information.per_process_user_time_limit = MAX_DOCUMENT_CPU_SECONDS * 10_000_000
    limits.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_PROCESS_TIME | JOB_OBJECT_LIMIT_PROCESS_MEMORY
    limits.process_memory_limit = MAX_DOCUMENT_RSS_BYTES
    if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise RuntimeError('无法设置文档资源限制。')
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        raise RuntimeError('无法应用文档资源限制。')
    return cast(object, job)


def main() -> None:
    """限制资源后输出结构化结果；Windows 使用 Job Object 并由父进程补充监控。"""
    windows_limits = _apply_windows_limits()
    if sys.platform != 'win32':
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (10, 12))
        if sys.platform != 'darwin':
            resource.setrlimit(resource.RLIMIT_DATA, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16_000_000, 16_000_000))
        if sys.platform != 'darwin':
            resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    logging.disable(logging.CRITICAL)
    from general_agent.documents import parse_docx, parse_pdf

    path, extension = Path(sys.argv[1]), sys.argv[2]
    result = parse_pdf(path) if extension == '.pdf' else parse_docx(path)
    payload = result.model_dump_json().encode('utf-8') + b'\n'
    if len(payload) > MAX_DOCUMENT_OUTPUT_BYTES:
        raise RuntimeError('文档解析结果超过输出资源限制。')
    sys.stdout.buffer.write(payload)


if __name__ == '__main__':
    main()
