"""校验显式选择的 Skill 名称，不在后端重复构造用户消息。"""

from general_agent.skills.catalog import SkillCatalog


def selected_skill_names(catalog: SkillCatalog, names: list[str], content: str | None = None) -> list[str]:
    """去重并验证名称；提供用户正文时要求完整 SKILL.md 已由前端放入消息。"""
    entries = {item['name'] for item in catalog.entries()}
    selected: list[str] = []
    for name in dict.fromkeys(names):
        if name not in entries:
            raise ValueError(f'Skill {name} 已不可用，请取消选择或刷新列表后重试。')
        if content is not None:
            item = catalog.read(name, 'SKILL.md')
            if item['content'] not in content:
                raise ValueError(f'Skill {name} 的完整 SKILL.md 未放入用户消息，请重新选择后发送。')
        selected.append(name)
    return selected
