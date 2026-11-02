"""从 AgentState 的 Skill 清单生成每轮可信路径说明。"""

from general_agent.agent.state import AgentState
from general_agent.skills.catalog import SkillCatalog


def build_skill_context(state: AgentState, catalog: SkillCatalog) -> list[str]:
    """返回完整 Skill 目录与当前 Run 可访问根目录，不重复注入正文。"""
    result = [catalog.prompt()]
    loaded = state['loaded_skills']
    if not loaded:
        return result
    roots = '\n'.join(
        f"- {skill['name']}:\n  root={skill['root']}\n  entry={skill['entry']}"
        for skill in loaded
    )
    result.append(
        'Skill 运行说明：所有 work 文件路径都相对于 /workspace；'
        '只允许访问下列已加载 Skill 根目录。\n' + roots + '\n'
        '工作状态直接保存在 Agent messages、workspace 和生成文件中。'
        '命令异步启动后必须轮询真实退出码。'
        '需要查看渲染结果时使用 view_work_image；'
        '上游要求用户交互时使用 open_skill_preview；'
        '导出并检查后使用 publish_skill_file 提供真实下载。'
    )
    return result
