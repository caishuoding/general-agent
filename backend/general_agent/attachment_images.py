"""验证图片原文件，并把当前会话明确选择的图片转为模型材料。"""

import warnings
from pathlib import Path
from typing import TypedDict

from PIL import Image, UnidentifiedImageError


class ImageMetadata(TypedDict):
    """仅保存实际解码确认的图片格式和尺寸，不保存模型识别推断。"""
    format: str
    mime: str
    width: int
    height: int


def validate_image(path: Path, extension: str) -> ImageMetadata:
    """先校验容器再重开完整解码，拒绝损坏、伪装格式、动画和超大像素图片。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path, formats=['PNG', 'JPEG']) as candidate:
                expected = 'PNG' if extension == '.png' else 'JPEG'
                if candidate.format != expected:
                    raise ValueError('图片实际格式与扩展名不一致，请使用正确的 PNG 或 JPEG 文件。')
                if candidate.width * candidate.height > 40_000_000:
                    raise ValueError('图片解码尺寸超过 4,000 万像素，请缩小图片后重新上传。')
                if getattr(candidate, 'n_frames', 1) != 1:
                    raise ValueError('暂不支持多帧图片，请将需要分析的帧单独导出为 PNG 或 JPEG。')
                metadata: ImageMetadata = {'format': expected, 'mime': 'image/png' if expected == 'PNG' else 'image/jpeg', 'width': candidate.width, 'height': candidate.height}
                candidate.verify()
            with Image.open(path, formats=['PNG', 'JPEG']) as decoded:
                decoded.load()
            return metadata
    except ValueError:
        raise
    except (OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValueError('图片损坏、解码失败或尺寸过大，请上传完整的 PNG 或 JPEG 图片。') from None
