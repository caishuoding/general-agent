"""验证视觉适配保留原件，并满足智谱大小与尺寸限制。"""

import base64
from io import BytesIO
import os
from pathlib import Path

from PIL import Image
import pytest

from general_agent.attachments.zhipu_images import image_url_part, MAX_IMAGE_BYTES, MAX_IMAGE_SIDE


@pytest.mark.parametrize('large_bytes', [False, True])
def test_large_images_are_adapted_without_modifying_original(tmp_path: Path, large_bytes: bool) -> None:
    """过宽或超过 5 MB 的图片仍能读图，发送副本遵循官方限制且原件保持原样。"""
    image = (Image.frombytes('RGB', (1400, 1400), os.urandom(1400 * 1400 * 3)) if large_bytes
             else Image.new('RGB', (6100, 10), 'red'))
    path = tmp_path / 'large.png'
    image.save(path)
    original = path.read_bytes()
    if large_bytes:
        assert len(original) > MAX_IMAGE_BYTES
    part = image_url_part(path, 'image/png')
    header, encoded = part['image_url']['url'].split(',', 1)
    assert header == 'data:image/jpeg;base64'
    sent = base64.b64decode(encoded)
    assert len(sent) <= MAX_IMAGE_BYTES
    with Image.open(BytesIO(sent)) as decoded:
        assert max(decoded.size) <= MAX_IMAGE_SIDE
        assert decoded.width / decoded.height == pytest.approx(image.width / image.height, rel=0.1)
    assert path.read_bytes() == original


def test_small_image_keeps_exact_original_bytes(tmp_path: Path) -> None:
    """无需转换的图片逐字节发送，避免无故损失清晰度。"""
    path = tmp_path / 'small.png'
    Image.new('RGB', (20, 20), 'red').save(path)
    part = image_url_part(path, 'image/png')
    assert part['image_url']['url'] == 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode()
