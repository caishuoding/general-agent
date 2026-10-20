"""核验模型分词器选择、字符回退和原文读取的实际剩余预算。"""

import json
from pathlib import Path

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from general_agent.infrastructure.settings import StoredSettings
from general_agent.infrastructure.tokens import TokenCounter


def test_matching_tokenizer_is_used_without_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """明确匹配的分词器优先，加载时移除预设截断；其他模型不能误用该计数器。"""
    path = tmp_path / 'tokenizer.json'
    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'hello': 1, 'world': 2}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.enable_truncation(max_length=1)
    tokenizer.save(str(path))
    monkeypatch.setenv('MODEL_TOKENIZERS', json.dumps({'matched-model': str(path)}))
    matching = TokenCounter('matched-model')
    assert matching.method == 'official_tokenizer'
    assert matching.count('hello world') == 2
    other = TokenCounter('other-model')
    assert other.method == 'character_estimate'
    assert other.count('中文A') == 7


def test_available_budget_includes_tools_and_existing_results() -> None:
    """已有工具结果和工具定义必须占用窗口，输出空间预留后预算不能为负。"""
    settings = StoredSettings(base_url='https://example.com', model='unknown', api_key='test',
                              context_window_tokens=4096, output_reserve_tokens=1000)
    counter = TokenCounter(settings.model)
    empty = counter.available(settings, [], [])
    occupied = counter.available(settings, [{'role': 'tool', 'content': '原文' * 200}], [{'description': '工具参数'}])
    assert 0 < occupied < empty < 4096 - 1000
    assert counter.available(settings, [{'role': 'tool', 'content': '正文' * 2000}], []) == 0
