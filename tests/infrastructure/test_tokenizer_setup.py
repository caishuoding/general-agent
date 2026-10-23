"""核验官方资产安装在下载内容校验失败时保留现有配置。"""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

from general_agent.infrastructure.tokenizer_setup import install_deepseek


def test_unverified_download_cannot_replace_existing_registration(tmp_path: Path) -> None:
    """模拟外部下载内容改变，安装应拒绝写入官方身份且保留原注册。"""
    existing = '{"models": []}'
    (tmp_path / 'tokenizers.json').write_text(existing)
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr('deepseek_v4_tokenizer/tokenizer.json', '{}')

    def download(request: httpx.Request) -> httpx.Response:
        """只模拟官方 HTTP 下载边界，返回未经认可的资产数据。"""
        return httpx.Response(200, content=output.getvalue())

    with pytest.raises(ValueError, match='校验失败'):
        install_deepseek(tmp_path, transport=httpx.MockTransport(download))
    assert (tmp_path / 'tokenizers.json').read_text() == existing
    assert not (tmp_path / 'tokenizers/deepseek-v4/tokenizer.json').exists()
