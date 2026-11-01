"""验证前端读取完整 SKILL.md 的公开接口。"""

from pathlib import Path

import pytest

from tests.auth.support import authenticated_client


def test_skill_content_endpoint_returns_complete_entry(database_url: str, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """显式选择只需要技能名称即可取得完整入口，不创建任何制作任务。"""
    skill = tmp_path / 'skills' / 'demo'
    skill.mkdir(parents=True)
    content = '---\nname: demo\ndescription: 演示技能\nmetadata:\n  version: "1"\n---\n# 完整正文\n逐步执行。\n'
    (skill / 'SKILL.md').write_text(content, encoding='utf-8')
    monkeypatch.setenv('SKILLS_DIR', str(tmp_path / 'skills'))

    with authenticated_client(database_url, tmp_path) as client:
        response = client.get('/api/skills/demo/content')

    assert response.status_code == 200, response.text
    assert response.json() == {
        'name': 'demo',
        'version': '1',
        'path': 'SKILL.md',
        'content': content,
        'digest': response.json()['digest'],
    }
