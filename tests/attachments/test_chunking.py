"""验证递归切片的语义边界、句子重叠和无损位置范围。"""

from random import Random

import pytest

from general_agent.infrastructure.tokens import TokenCounter

from general_agent.attachments.knowledge import ChunkPolicy, chunk_ranges, sentence_ends, text_units


@pytest.fixture(autouse=True)
def character_token_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """用一字符一 Token 的可控计数隔离边界算法；真实分词另有专项测试。"""
    def count_characters(self: TokenCounter, text: str) -> int:
        """为本模块提供确定的计量单位，保留已有边界样本的可读性。"""
        return len(text)
    monkeypatch.setattr(TokenCounter, 'count', count_characters)


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_natural_paragraphs_preserve_soft_lines_and_original_characters(newline: str) -> None:
    """空行划段，段内换行原样保留，CRLF 不能被误识别为空行。"""
    text = f'首行{newline}续行{newline} {newline}下一段{newline}'
    units = text_units(text)
    assert len(units) == 2
    assert units[0].content == f'首行{newline}续行{newline} {newline}'
    assert ''.join(unit.content for unit in units) == text


def test_short_paragraphs_merge_before_splitting_sentences() -> None:
    """优先完整合并短段落，装不下后续段落时在前段末尾结束。"""
    text = '甲' * 45 + '。\n\n' + '乙' * 45 + '。\n\n' + '丙' * 45 + '。'
    units = text_units(text)
    ends: list[int] = []
    for unit in units:
        ends.append((ends[-1] if ends else 0) + len(unit.content))
    ranges = chunk_ranges(text, ChunkPolicy(100, overflow=0, overlap=0), ends)
    assert ranges == [(0, 96), (96, len(text))]


def test_overlap_does_not_repeat_a_sentence_above_twenty_percent() -> None:
    """完整句子超过重叠硬上限时不重复该句，不能为完整性突破百分之二十。"""
    text = ''.join(character * 39 + '。' for character in '甲乙丙丁')
    ranges = chunk_ranges(text, ChunkPolicy(100, overflow=0, overlap=30))
    assert ranges == [(0, 80), (80, 160)]


def test_defaults_and_explicit_overlap_are_capped_at_twenty_percent() -> None:
    """默认八百 Token、重叠一百六十 Token；旧配置和自定义配置均不能超过五分之一。"""
    assert ChunkPolicy().tokens == 800
    assert ChunkPolicy().overlap == 160
    assert ChunkPolicy(800, overlap=200).overlap == 160
    assert ChunkPolicy(100, overflow=0, overlap=10000).overlap == 20
    assert ChunkPolicy(100, overflow=0, overlap=0).overlap == 0


def test_overlap_shrinks_before_breaking_a_sentence_that_fits() -> None:
    """重叠占用空间时优先减少重叠，不拆开本来能放进单片的新句。"""
    text = '甲' * 59 + '。' + '乙' * 119 + '。'
    assert chunk_ranges(text, ChunkPolicy(150, overflow=0, overlap=60)) == [(0, 60), (60, 180)]


def test_long_sentences_fall_back_to_clauses_then_characters() -> None:
    """超长句先按分句断开，无标点时硬切，始终满足包含重叠的上限。"""
    text = '甲' * 59 + '，' + '乙' * 59 + '，' + '丙' * 59 + '。'
    assert chunk_ranges(text, ChunkPolicy(100, overflow=0, overlap=0)) == [(0, 60), (60, 120), (120, 180)]
    assert chunk_ranges('甲' * 250, ChunkPolicy(100, overflow=0, overlap=20)) == [(0, 100), (80, 180), (160, 250)]


def test_headings_prevent_cross_section_chunks_and_overlap() -> None:
    """章节开始时关闭前片并清空重叠，不把不同章节混入同一片。"""
    text = '# 第一节\n' + '甲。' * 30 + '\n# 第二节\n' + '乙。' * 30
    boundary = text.index('# 第二节')
    ranges = chunk_ranges(text, ChunkPolicy(100, overflow=0, overlap=20))
    assert ranges == [(0, boundary), (boundary, len(text))]


def test_sentence_boundaries_handle_quotes_decimals_and_abbreviations() -> None:
    """句末保留闭引号，不把数字小数点或常见英文称谓当成句末。"""
    text = 'Dr. Smith paid 3.14 dollars. 他说：“收到。”\n下一句！'
    ends = sentence_ends(text)
    assert [text[start:end] for start, end in zip([0, *ends[:-1]], ends)] == [
        'Dr. Smith paid 3.14 dollars. ', '他说：“收到。”\n', '下一句！']


def test_varied_inputs_have_no_gaps_and_always_advance() -> None:
    """混合中英文、空行及无标点长句均无遗漏，重叠不导致原地循环。"""
    random = Random(42)
    for _ in range(100):
        text = ''.join(random.choice(['中文。', 'English sentence! ', '\n\n', '无标点' * 70]) for _ in range(30))
        policy = ChunkPolicy(random.choice([100, 200, 800]), overflow=0, overlap=random.choice([0, 20, 200]))
        ranges = chunk_ranges(text, policy)
        previous_end = 0
        restored = ''
        for start, end in ranges:
            assert start <= previous_end < end
            assert end - start <= policy.tokens
            assert previous_end - start <= policy.tokens // 5
            restored += text[previous_end:end]
            previous_end = end
        assert restored == text


def test_invalid_overlap_and_excessive_lines_are_rejected() -> None:
    """拒绝非法重叠配置，按自然段聚合也不能绕过原行数资源限制。"""
    with pytest.raises(ValueError, match='重叠'):
        ChunkPolicy(overlap=-1)
    with pytest.raises(ValueError, match='资源限制'):
        text_units('x\n' * 20001)
