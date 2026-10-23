"""安装固定来源及校验值的 DeepSeek 官方分词数据，不执行下载包中的代码。"""

import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from zipfile import ZipFile

import httpx
from tokenizers import Tokenizer

from general_agent.infrastructure.tokens import TokenCounter, _Registration, _Registry

OFFICIAL_URL = 'https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip'
OFFICIAL_SHA256 = '89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614'
TOKENIZER_PATH = Path('tokenizers/deepseek-v4/tokenizer.json')
MAX_BYTES = 16 * 1024 * 1024


def _atomic_write(path: Path, data: bytes) -> None:
    """以私有权限原子写入数据或注册文件，避免运行中读取半份配置。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_deepseek(directory: Path, *, base_url: str = 'https://api.deepseek.com',
                     transport: httpx.BaseTransport | None = None) -> TokenCounter:
    """安装并注册 deepseek-flash 官方文件，保留其他模型注册，重复执行复用已校验资产。"""
    registry_path = directory / 'tokenizers.json'
    registry = _Registry.model_validate_json(registry_path.read_bytes()) if registry_path.exists() else _Registry(models=[])
    destination = directory / TOKENIZER_PATH
    data = destination.read_bytes() if destination.exists() else b''
    if sha256(data).hexdigest() != OFFICIAL_SHA256:
        with httpx.Client(transport=transport, timeout=60, follow_redirects=True) as client:
            with client.stream('GET', OFFICIAL_URL) as response:
                response.raise_for_status()
                archive = bytearray()
                for part in response.iter_bytes():
                    archive.extend(part)
                    if len(archive) > MAX_BYTES:
                        raise ValueError('官方分词器下载包超过大小限制。')
        with ZipFile(BytesIO(archive)) as bundle:
            member = bundle.getinfo('deepseek_v4_tokenizer/tokenizer.json')
            if member.file_size > MAX_BYTES:
                raise ValueError('官方分词器数据超过大小限制。')
            data = bundle.read(member)
        if sha256(data).hexdigest() != OFFICIAL_SHA256:
            raise ValueError('官方分词器文件校验失败，未修改现有配置。')
        Tokenizer.from_str(data.decode('utf-8'))
        _atomic_write(destination, data)
    entry = _Registration(base_url=base_url.rstrip('/'), model='deepseek-flash', adapter='huggingface',
                          path=TOKENIZER_PATH.as_posix(), sha256=OFFICIAL_SHA256, source=OFFICIAL_URL)
    registry.models = [item for item in registry.models if (item.base_url.rstrip('/'), item.model) != (entry.base_url, entry.model)]
    registry.models.append(entry)
    _atomic_write(registry_path, registry.model_dump_json(indent=2).encode('utf-8'))
    return TokenCounter(entry.model, base_url=entry.base_url, directory=directory)


def main() -> None:
    """提供可重复安装入口，打印不含凭据的资产身份与离线计数结果。"""
    parser = argparse.ArgumentParser(description='安装 DeepSeek 官方分词器')
    parser.add_argument('--directory', type=Path, default=Path(os.environ.get('APP_DATA_DIR', '.local')))
    parser.add_argument('--base-url', default='https://api.deepseek.com')
    args = parser.parse_args()
    counter = install_deepseek(args.directory, base_url=args.base_url)
    print(json.dumps({'model': counter.model, 'method': counter.method, 'fingerprint': counter.fingerprint,
                      'sample': '你好，世界！', 'tokens': counter.count('你好，世界！')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
