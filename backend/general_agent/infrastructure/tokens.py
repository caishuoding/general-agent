"""按模型选择本地官方 Tokenizer，缺少对应实现时使用保守字符估算。"""

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

from tokenizers import Tokenizer

from general_agent.infrastructure.settings import StoredSettings


class TokenCounter:
    """只加载明确匹配模型的官方 tokenizer.json，不猜用其他模型的分词器。"""

    def __init__(self, model: str) -> None:
        """加载明确匹配模型的可选官方分词器，并记录计数配置指纹供切片重建使用。"""
        self.model = model
        self.tokenizer: Tokenizer | None = None
        paths = json.loads(os.environ.get('MODEL_TOKENIZERS', '{}'))
        path = paths.get(model)
        if path:
            self.tokenizer = Tokenizer.from_file(path)
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
        self.method = 'official_tokenizer' if self.tokenizer is not None else 'character_estimate'
        identity = Path(path).read_bytes() if path else b'utf8-conservative-v1'
        self.fingerprint = sha256(model.encode() + b'\n' + identity).hexdigest()

    def count(self, text: str) -> int:
        """官方按普通文本编码计数；无对应实现时每个字符按其 UTF-8 字节数保守估算。"""
        if self.tokenizer is not None:
            return len(self.tokenizer.encode(text, add_special_tokens=False).ids)
        return sum(len(character.encode('utf-8')) for character in text)

    def available(self, settings: StoredSettings, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        """按执行点实际消息和工具定义扣除窗口占用、回答预留及协议包装余量。"""
        serialized = json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False)
        return max(0, settings.context_window_tokens - settings.output_reserve_tokens - self.count(serialized) - 512)

    def prefix(self, text: str, budget: int) -> str:
        """寻找预算内的连续字符前缀；只切字符边界，并核验返回文本的实际计数。"""
        if budget <= 0:
            return ''
        if self.count(text) <= budget:
            return text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.count(text[:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        return text[:low]
