"""验证官方资产安装的完整性边界，不调用真实下载服务。"""

from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from tokenizers import Tokenizer, models

from general_agent.infrastructure import tokenizer_setup


def test_unverified_download_cannot_replace_registry(tmp_path: Path) -> None:
    """错误内容即使 HTTP 成功也不能注册为官方模型或破坏现有配置。"""
    registry = tmp_path / 'tokenizers.json'
    registry.write_text('{"models":[]}')

    def download(request: httpx.Request) -> httpx.Response:
        """提供成功 HTTP 状态但完整性不符的资产。"""
        assert str(request.url) == tokenizer_setup.OFFICIAL_URL
        return httpx.Response(200, content=b'wrong asset')

    with pytest.raises(ValueError, match='校验失败'):
        tokenizer_setup.install_glm(tmp_path, transport=httpx.MockTransport(download))
    assert registry.read_text() == '{"models":[]}'
    assert not (tmp_path / tokenizer_setup.TOKENIZER_PATH).exists()


def test_verified_local_asset_is_reused_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """合法本地资产在重复安装中无需网络，并按精确智谱模型注册。"""
    asset = Tokenizer(models.WordLevel({'[UNK]': 0, 'hello': 1}, unk_token='[UNK]')).to_str().encode()
    monkeypatch.setattr(tokenizer_setup, 'OFFICIAL_SHA256', sha256(asset).hexdigest())
    target = tmp_path / tokenizer_setup.TOKENIZER_PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(asset)

    def offline(request: httpx.Request) -> httpx.Response:
        """复用已校验资产时任何网络请求都表示错误。"""
        raise AssertionError('重复安装不应联网。')

    for _ in range(2):
        counter = tokenizer_setup.install_glm(tmp_path, transport=httpx.MockTransport(offline))
        assert counter.method == 'official_tokenizer'
        assert counter.model == 'glm-5.3-flash'
        assert counter.count('hello') == 1
