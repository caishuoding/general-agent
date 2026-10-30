"""把用户显式选择解析为稳定快照，并在模型边界拼接完整描述。"""

from typing import TypedDict

from general_agent.skills.catalog import SkillCatalog


class SkillSnapshot(TypedDict):
    """保存显式选择的完整描述及稳定版本，供模型展开和历史重试使用。"""

    name: str
    description: str
    version: str
    digest: str


def selected_skills(catalog: SkillCatalog, names: list[str], content: str) -> list[SkillSnapshot]:
    """按选择顺序解析已安装描述，拒绝未知项和超长消息，不接受客户端描述。"""
    entries = {item['name']: item for item in catalog.entries()}
    selected: list[SkillSnapshot] = []
    for name in dict.fromkeys(names):
        if name not in entries:
            raise ValueError(f'Skill {name} 已不可用，请取消选择或刷新列表后重试。')
        entry = entries[name]
        selected.append({'name': entry['name'], 'description': entry['description'],
                         'version': entry['version'], 'digest': entry['digest']})
    if len(user_skill_content(content, selected)) > 32000:
        raise ValueError('所选技能的完整描述与消息超过长度限制，请减少选择或缩短需求。')
    return selected


def user_skill_content(content: str, selections: list[SkillSnapshot]) -> str:
    """用保存的完整描述和原文组成一条 user 消息，空选择原样返回正文。"""
    if not selections:
        return content
    blocks = ['本次明确指定的 Skills：']
    for selection in selections:
        blocks.append(f'Skill：{selection["name"]}\nDescription：\n{selection["description"]}')
    blocks.append('用户需求：\n' + content)
    return '\n\n'.join(blocks)
