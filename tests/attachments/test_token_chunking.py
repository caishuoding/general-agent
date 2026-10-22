"""验证按 Token 的软上限、完整块容差，以及 Markdown 表头的完整预算。"""

from pathlib import Path

import pytest
from docx import Document

from general_agent.attachments.knowledge import ChunkPolicy, chunk_ranges, chunk_specs
from general_agent.attachments.word_structure import word_units
from general_agent.infrastructure.tokens import TokenCounter


def test_complete_paragraph_can_extend_to_one_thousand_tokens() -> None:
    """完整段落在八百以上、一千以内时保留整片，不按字符数强行切开。"""
    policy = ChunkPolicy()
    assert policy.tokens == 800
    assert policy.max_tokens == 1000
    assert chunk_ranges('A' * 900, policy) == [(0, 900)]
    text = 'A' * 900 + '\n\n' + 'B' * 900
    ranges = chunk_ranges(text, policy, [902, len(text)])
    assert ranges[0] == (0, 902)
    assert all(policy.counter.count(text[start:end]) <= 1000 for start, end in ranges)


def test_very_long_unbroken_text_does_not_spend_tolerance_arbitrarily() -> None:
    """没有完整段句边界的长串仍以目标预算切开，不为凑满而任意使用两百容差。"""
    policy = ChunkPolicy(overlap=0)
    assert chunk_ranges('A' * 1800, policy) == [(0, 800), (800, 1800)]


def test_token_count_not_character_count_limits_chinese() -> None:
    """未配置官方分词器时显式使用现有保守 Token 估算，中文不能当作一字一 Token。"""
    policy = ChunkPolicy(counter=TokenCounter('unconfigured-test'))
    text = '中文' * 500
    assert policy.counter.count(text) > len(text)
    ranges = chunk_ranges(text, policy)
    assert len(ranges) > 1
    assert all(policy.counter.count(text[start:end]) <= 1000 for start, end in ranges)
    for left, right in zip(ranges, ranges[1:]):
        assert policy.counter.count(text[right[0]:left[1]]) <= 160


def test_each_table_chunk_is_markdown_and_repeats_budgeted_header() -> None:
    """长表按完整行拆分，每片都有标准 Markdown 表头，表头和语法计入 Token 上限。"""
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = 'Name', 'Value'
    for index in range(60):
        row = table.add_row()
        row.cells[0].text, row.cells[1].text = f'ITEM{index:02d}', 'A' * 24
    units = word_units(doc)
    text = ''.join(unit.content for unit in units)
    policy = ChunkPolicy()
    specs = chunk_specs(units, policy)
    assert len(specs) > 1
    rendered = [spec.prefix + text[spec.start:spec.end] + spec.suffix for spec in specs]
    for content in rendered:
        assert content.startswith('| Name | Value |\n| --- | --- |\n')
        assert policy.counter.count(content) <= policy.max_tokens
        assert all(line.startswith('|') and line.endswith('|') for line in content.strip().splitlines())
    assert sum(content.count('ITEM00') for content in rendered) == 1
    assert sum(content.count('ITEM59') for content in rendered) == 1


def test_table_cell_pipes_newlines_and_oversized_row_keep_markdown_shape() -> None:
    """单元格特殊符号转义，超长单行按单元格续片，所有续片仍有完整表头和列数。"""
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = 'Name', 'Value'
    table.cell(1, 0).text = 'A|B\nC'
    table.cell(1, 1).text = ('D' * 24 + '|&<>\n') * 100
    units = word_units(doc)
    text = ''.join(unit.content for unit in units)
    assert 'A\\|B<br>C' in text
    policy = ChunkPolicy()
    rendered = [spec.prefix + text[spec.start:spec.end] + spec.suffix for spec in chunk_specs(units, policy)]
    assert all(content.startswith('| Name | Value |\n| --- | --- |\n') for content in rendered)
    assert all(policy.counter.count(content) <= 1000 for content in rendered)
    assert sum(content.count('D') for content in rendered) == 2400
    assert sum(content.count('\\|') for content in rendered) == 101
    assert sum(content.count('&amp;') for content in rendered) == 100
    assert sum(content.count('&lt;') for content in rendered) == 100
    assert sum(content.count('&gt;') for content in rendered) == 100
    assert sum(content.count('<br>') for content in rendered) == 101


def test_configured_tokenizer_controls_budget_instead_of_text_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置分词器后按实际编码计数，长于一千字符但不足八百 Token 的段落应保持完整。"""
    import json
    from tokenizers import Tokenizer, models, pre_tokenizers

    tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'word': 1}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path / 'fixture-tokenizer.json'
    tokenizer.save(str(path))
    monkeypatch.setenv('MODEL_TOKENIZERS', json.dumps({'fixture-model': str(path)}))
    counter = TokenCounter('fixture-model')
    policy = ChunkPolicy(counter=counter)
    text = 'word ' * 900
    assert counter.count(text) == 900
    assert len(text) > 1000
    assert counter.method == 'official_tokenizer'
    assert chunk_ranges(text, policy) == [(0, len(text))]
