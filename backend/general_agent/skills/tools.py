"""公开 Skill 选择和制作任务工具，所有身份由后端绑定。"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SkillInput(BaseModel):
    """限定加载名称和可选的显式续接任务，不接受模型指定安装路径。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str
    task_id: str | None = None


class TaskInput(BaseModel):
    """所有工作工具必须明确指出已激活的任务。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    task_id: str


class ReadSkillInput(TaskInput):
    """区分按需加载可信 Markdown 指令与读取脚本、索引等参考数据。"""
    path: str
    instruction: bool = True


class ContextInput(TaskInput):
    """保留当前阶段已读指令，显式卸载其他阶段文档以管理上下文。"""
    paths: list[str]


class ReadFileInput(TaskInput):
    """读取目录或文本，范围受限且保留后续位置。"""
    path: str = '.'
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20000, ge=1, le=100000)


class WriteFileInput(TaskInput):
    """写入任务目录中的文本文件。"""
    path: str
    content: str = Field(max_length=2_000_000)


class CommandInput(TaskInput):
    """在隔离任务容器异步执行命令，不提供 Docker 或宿主参数。"""
    command: str = Field(min_length=1, max_length=30000)
    seconds: int = Field(default=300, ge=1, le=900)


class PollInput(TaskInput):
    """读取实际执行状态及有界日志，可短暂等待以免模型空轮询。"""
    command_id: str
    offset: int = Field(default=0, ge=0)
    wait_seconds: int = Field(default=10, ge=0, le=20)


class StateInput(TaskInput):
    """保存工作流阶段和面向用户的等待状态。"""
    status: str
    stage: str = Field(max_length=2000)
    route: str = Field(default='', max_length=100)


class ImportInput(TaskInput):
    """将用户有权使用的会话附件或已选知识库原件复制到任务输入目录。"""
    source_id: str
    kind: str = 'attachment'


class PathInput(TaskInput):
    """选择真实工作文件用于查看或发布。"""
    path: str


class PreviewInput(TaskInput):
    """通过真实项目预览锁发现端口，不允许模型指定任意主机。"""
    project_path: str


DEFINITIONS: list[tuple[str, str, type[BaseModel]]] = [
    ('load_skill', '按完整 description 选择已安装 Skill 后加载完整入口。续接必须传原 task_id，新任务省略。介绍问题不授权开始制作。', SkillInput),
    ('read_skill_file', '读取已激活 Skill 中的相对文件。Markdown 工作流使用 instruction=true，脚本或索引使用 false；可信指令将在下一轮完整注入。', ReadSkillInput),
    ('set_skill_context', '保留 paths 中已经完整加载的阶段指令，卸载其余支持文件；SKILL.md 始终保留。跨阶段切换时按上游要求管理，不能丢弃仍适用的强制规则。', ContextInput),
    ('read_work_file', '列出任务目录或分段读取 UTF-8 文本，path 相对于 /workspace；根据 next_offset 续读。', ReadFileInput),
    ('write_work_file', '在当前任务 /workspace 内创建或修改文本，不接受宿主路径。', WriteFileInput),
    ('run_skill_command', '在隔离任务容器 /workspace 异步执行 shell 命令，返回 command_id，之后用 poll_skill_command 取得真实退出码；读取 Skill 后按其命令执行。', CommandInput),
    ('poll_skill_command', '等待并读取当前任务命令的真实状态和有界日志。running 表示尚未完成，非零退出码必须检查并按工作流修复。', PollInput),
    ('save_skill_task', '保存当前工作流、阶段和任务状态。需要用户确认时设 waiting 并结束当前回答，收到确认后显式续接；完成设 completed。', StateInput),
    ('import_skill_source', '将本会话有效 attachment 原件或当前已选知识库 knowledge 原件复制到工作目录；返回原文件名和实际路径。', ImportInput),
    ('view_work_image', '将任务内真实 PNG/JPEG 图片发送给视觉模型检查；SVG/PDF 先在隔离环境渲染为 PNG。', PathInput),
    ('publish_skill_file', '将真实工作文件保存为不可变交付副本并返回下载地址；必须实际导出并通过上游检查后发布，不能伪造链接。', PathInput),
    ('open_skill_preview', '上游预览服务成功启动后，通过 project_path 下 live_preview/lock.json 发现真实端口并返回用户可访问的受保护预览入口。', PreviewInput),
]
SKILL_TOOLS = {name for name, _, _ in DEFINITIONS}


def skill_tools() -> list[dict[str, Any]]:
    """返回严格参数定义，后端执行仍逐次验证激活身份和会话权限。"""
    return [{'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': model.model_json_schema()}} for name, description, model in DEFINITIONS]


def validate(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """统一校验工具参数并规范化 UUID，拒绝额外字段与错误类型。"""
    model = next(model for key, _, model in DEFINITIONS if key == name)
    result = model.model_validate(arguments).model_dump()
    for key in ('task_id', 'command_id', 'source_id'):
        if result.get(key):
            result[key] = UUID(result[key])
    return result
