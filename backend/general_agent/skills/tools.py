"""公开 Skill 加载、按 Run 工作区和生成文件工具。"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from general_agent.infrastructure.agent_limits import LIMITS


class SkillInput(BaseModel):
    """按真实安装名称加载入口，不接受文件系统根目录。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str


class SkillFileInput(BaseModel):
    """读取当前会话已使用 Skill 根目录内的相对文本文件。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    skill: str
    path: str


class ReadFileInput(BaseModel):
    """读取当前会话 workspace 的目录或文本，范围受限并保留续读位置。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str = '.'
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20000, ge=1, le=100000)


class WriteFileInput(BaseModel):
    """写入当前会话 workspace 中的相对文本文件。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str
    content: str = Field(max_length=2_000_000)


class CommandInput(BaseModel):
    """在当前 Run 的隔离容器中异步执行命令。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    command: str = Field(min_length=1, max_length=30000)
    seconds: int = Field(default=LIMITS.skill_command_default_seconds, ge=1, le=LIMITS.skill_command_seconds)


class PollInput(BaseModel):
    """读取当前 Run 命令的真实状态和有界日志。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    command_id: str
    offset: int = Field(default=0, ge=0)
    wait_seconds: int = Field(default=10, ge=0, le=LIMITS.skill_poll_seconds)


class ImportInput(BaseModel):
    """将用户有权使用的会话附件或知识库原件复制到当前会话 workspace。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    source_id: str
    kind: str = 'attachment'


class PathInput(BaseModel):
    """选择当前会话 workspace 中的真实文件用于检查或发布。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str


class PreviewInput(BaseModel):
    """通过当前 Run 项目预览锁发现端口，不允许模型指定任意主机。"""
    model_config = ConfigDict(extra='forbid', strict=True)
    project_path: str
    surface: Literal['preview', 'confirmation'] = 'preview'


DEFINITIONS: list[tuple[str, str, type[BaseModel]]] = [
    ('load_skill', '加载已安装 Skill 的完整 SKILL.md。返回内容不创建任务、目录或会话状态；可多次调用加载多个 Skill。', SkillInput),
    ('read_skill_file', '读取当前会话已使用 Skill 根目录内的相对文本文件；不同 Skill 使用各自固定根目录。', SkillFileInput),
    ('read_work_file', '列出当前会话工作目录或分段读取 UTF-8 文本，path 为 workspace 相对路径。', ReadFileInput),
    ('write_work_file', '在当前会话 workspace 内创建或修改文本，不接受宿主绝对路径。', WriteFileInput),
    ('run_skill_command', '在隔离容器 /workspace 异步执行 shell 命令，返回 command_id，之后用轮询取得真实退出码。', CommandInput),
    ('poll_skill_command', '等待并读取当前 Run 命令的真实状态和有界日志；非零退出码必须检查并修复。', PollInput),
    ('import_skill_source', '将本会话有效 attachment 原件或当前已选知识库原件复制到 workspace；返回相对路径。', ImportInput),
    ('view_work_image', '将当前 Run 中真实 PNG/JPEG 图片发送给视觉模型检查；SVG/PDF 先由上游脚本渲染。', PathInput),
    ('publish_skill_file', '将当前 Run 的真实工作文件保存为不可变交付副本并返回下载地址。', PathInput),
    ('open_skill_preview', '读取当前 Run 项目的预览锁并返回短期授权页面；surface=confirmation 使用确认页锁。', PreviewInput),
]
SKILL_TOOLS = {name for name, _, _ in DEFINITIONS}


def skill_tools() -> list[dict[str, Any]]:
    """返回严格参数定义，身份和目录始终由后端当前 Run 注入。"""
    return [{'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': model.model_json_schema()}} for name, description, model in DEFINITIONS]


def validate(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """统一校验工具参数并规范化 UUID，拒绝额外字段与错误类型。"""
    model = next(model for key, _, model in DEFINITIONS if key == name)
    result = model.model_validate(arguments).model_dump()
    for key in ('command_id', 'source_id'):
        if result.get(key):
            result[key] = UUID(result[key])
    return result
