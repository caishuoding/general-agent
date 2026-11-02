"""通过 Skill 公开加载接口验证完整描述和可信文件读取。"""

from pathlib import Path

import pytest

from general_agent.skills.catalog import SkillCatalog
from general_agent.skills.selection import selected_skill_names


def test_catalog_preserves_description_and_loads_complete_instruction(tmp_path: Path) -> None:
    """目录完整保留多行描述，激活返回原始入口及确定的运行路径。"""
    root = tmp_path / 'ppt-master'
    root.mkdir()
    content = '---\nname: ppt-master\ndescription: |\n  制作演示文稿。\n  也能添加旁白。\nmetadata:\n  version: "1.0"\n---\n# 规则\n完整正文。\n'
    (root / 'SKILL.md').write_text(content)
    catalog = SkillCatalog(tmp_path)
    assert catalog.entries()[0]['description'] == '制作演示文稿。\n也能添加旁白。\n'
    loaded = catalog.read('ppt-master', 'SKILL.md')
    assert loaded['content'] == content
    assert loaded['absolute_path'] == '/opt/skills/ppt-master/SKILL.md'
    assert loaded['version'] == '1.0'
    with pytest.raises(ValueError, match='路径'):
        catalog.read('ppt-master', '../secret.md')


def test_selected_skill_names_supports_multiple_without_duplicates(tmp_path: Path) -> None:
    """会话选择保留多个真实 Skill 名称并去重，不重新拼描述。"""
    for name in ('alpha', 'beta'):
        root = tmp_path / name
        root.mkdir()
        (root / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: {name}\n---\n# {name}\n',
            encoding='utf-8',
        )
    catalog = SkillCatalog(tmp_path)
    assert selected_skill_names(catalog, ['beta', 'alpha', 'beta']) == ['beta', 'alpha']
    with pytest.raises(ValueError, match='已不可用'):
        selected_skill_names(catalog, ['missing'])
