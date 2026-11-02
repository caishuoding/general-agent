"""构建智谱视觉消息，仅在模型发送时缩放副本，保留本地原件。"""

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image

from general_agent.infrastructure.provider import ImagePart

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_SIDE = 6000


def image_url_part(path: Path, mime: str) -> ImagePart:
    """返回图片 data URL；大图等比缩小或压缩到接口限制，本地原件不变。"""
    if not path.is_file():
        raise ValueError('图片原文件已不可用，请重新上传或重新处理。')
    data = path.read_bytes()
    with Image.open(BytesIO(data)) as source:
        if max(source.size) > MAX_IMAGE_SIDE or len(data) > MAX_IMAGE_BYTES:
            source.load()
            image = source.convert('RGBA')
            image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            background = Image.new('RGB', image.size, 'white')
            background.paste(image, mask=image.getchannel('A'))
            while True:
                output = BytesIO()
                background.save(output, format='JPEG', quality=90)
                data = output.getvalue()
                if len(data) <= MAX_IMAGE_BYTES:
                    break
                background = background.resize((max(1, background.width * 3 // 4),
                                                max(1, background.height * 3 // 4)))
            mime = 'image/jpeg'
    encoded = base64.b64encode(data).decode('ascii')
    return {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{encoded}'}}
