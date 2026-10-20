"""将 Word 自动排版为固定 PDF，所有临时输出仅留在本次转换目录。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def paginate_word(source: Path, destination: Path) -> None:
    """独立配置排版 DOCX，macOS 显式提供系统中文字体，转换失败时不发布副本。"""
    executable = os.environ.get('LIBREOFFICE_BINARY') or shutil.which('soffice')
    if not executable:
        raise ValueError('Word 自动分页需要 LibreOffice，请安装后重新处理附件。')
    with TemporaryDirectory(prefix='word-pagination-') as temporary:
        directory = Path(temporary)
        input_path = directory / 'source.docx'
        shutil.copyfile(source, input_path)
        profile = directory / 'profile'
        if sys.platform == 'darwin':
            # 部分无界面发行版只加载私有字体；缺字会破坏 PDF 的中文字符映射。
            system_font = Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
            if system_font.is_file():
                private_fonts = profile / 'user' / 'fonts'
                private_fonts.mkdir(parents=True)
                (private_fonts / 'ArialUnicode.ttf').symlink_to(system_font)
        # 独立用户配置避免并发转换互相抢占或复用桌面进程。
        try:
            result = subprocess.run(
                [executable, f'-env:UserInstallation={profile.as_uri()}', '--headless', '--nologo',
                 '--nodefault', '--norestore', '--convert-to', 'pdf:writer_pdf_Export',
                 '--outdir', str(directory), str(input_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Word 自动分页未完成，请检查排版运行时或简化文档后重新上传。') from None
        output = directory / 'source.pdf'
        if result.returncode != 0 or not output.is_file() or not 0 < output.stat().st_size <= 80_000_000:
            raise ValueError('Word 自动分页失败或副本超过 80 MB，请简化后重新上传。')
        shutil.copyfile(output, destination)
