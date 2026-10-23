"""核验模型分词器选择、字符回退和原文读取的实际剩余预算。"""

import json
from hashlib import sha256
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


def test_registry_routes_by_endpoint_and_model_without_changing_interface(tmp_path: Path) -> None:
    """同一模型别名在不同服务商下隔离，切换模型后仍使用相同计数和截取接口。"""
    path = tmp_path / 'tokenizer.json'
    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'hello': 1, 'world': 2}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.enable_truncation(max_length=1)
    tokenizer.enable_padding(length=20)
    tokenizer.save(str(path))
    (tmp_path / 'tokenizers.json').write_text(json.dumps({'models': [{
        'base_url': 'https://provider-one.example/v1', 'model': 'shared-name',
        'adapter': 'huggingface', 'path': 'tokenizer.json',
        'sha256': sha256(path.read_bytes()).hexdigest(), 'source': 'https://provider-one.example/tokenizer',
    }]}))
    settings = StoredSettings(base_url='https://provider-one.example/v1/', model='shared-name', api_key='test')
    counter = TokenCounter.for_settings(settings, tmp_path)
    assert counter.method == 'official_tokenizer'
    assert counter.count('hello world') == 2
    assert counter.prefix('hello world', 1) == 'hello '
    other = TokenCounter.for_settings(settings.model_copy(update={'base_url': 'https://provider-two.example/v1'}), tmp_path)
    assert other.method == 'character_estimate'
    assert other.count('hello world') == 11
    assert other.fingerprint != counter.fingerprint


def test_explicit_corrupt_tokenizer_is_not_silently_estimated(tmp_path: Path) -> None:
    """已注册的官方文件被替换时必须失败，不能静默切换成其他模型或估算。"""
    (tmp_path / 'tokenizer.json').write_text('{}')
    (tmp_path / 'tokenizers.json').write_text(json.dumps({'models': [{
        'base_url': 'https://provider.example', 'model': 'configured', 'adapter': 'huggingface',
        'path': 'tokenizer.json', 'sha256': '0' * 64, 'source': 'https://provider.example/tokenizer',
    }]}))
    settings = StoredSettings(base_url='https://provider.example', model='configured', api_key='test')
    with pytest.raises(ValueError, match='校验失败'):
        TokenCounter.for_settings(settings, tmp_path)
