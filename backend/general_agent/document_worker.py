"""在独立且受资源限额保护的进程中解析不可信文档。"""

import logging
import resource
import sys
from pathlib import Path


def main() -> None:
    """限制CPU和输出大小后输出结构化结果；Linux额外限制内存，macOS由父进程监测RSS。"""
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
    print(result.model_dump_json())


if __name__ == '__main__':
    main()
