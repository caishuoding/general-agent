"""执行静态注册的 Skill 工具，并在调用时校验路径和 Run 权限。"""

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import UUID, uuid5

from general_agent.agent.repository import RunCheckpoint
from general_agent.agent.state import AgentState, LoadedSkill
from general_agent.attachments.images import validate_image
from general_agent.attachments.zhipu_images import image_url_part
from general_agent.skills.files import SafeFiles
from general_agent.skills.runtime import SkillRuntime
from general_agent.skills.tools import validate


@dataclass(frozen=True)
class SkillToolExecution:
    """保存 Skill 工具结果及需要同步到 State 的附加信息。"""

    result: dict[str, Any]
    loaded_skill: LoadedSkill | None = None
    image_messages: list[dict[str, Any]] = field(default_factory=list)


def execute_skill_tool(
    state: AgentState,
    checkpoint: RunCheckpoint,
    runtime: SkillRuntime,
    name: str,
    arguments: dict[str, Any],
) -> SkillToolExecution:
    """执行一个 Skill 工具；注册始终可见，具体路径和加载状态在此校验。"""
    values = validate(name, arguments)
    checkpoint.guard()
    loaded = {skill['name'] for skill in state['loaded_skills']}
    if name == 'load_skill':
        skill_name = str(values['name'])
        item = runtime.service.catalog.read(skill_name, 'SKILL.md')
        with runtime.service.database.connect() as connection:
            runtime.service.remember_skills(connection, checkpoint.conversation_id, [skill_name])
        access: LoadedSkill = {
            'name': skill_name,
            'version': item['version'],
            'root': f'/opt/skills/{skill_name}',
            'entry': f'/opt/skills/{skill_name}/SKILL.md',
        }
        return SkillToolExecution({
            'name': skill_name,
            'version': item['version'],
            'skill_dir': access['root'],
            'content': item['content'],
            'digest': item['file_digest'],
        }, loaded_skill=access)
    if name == 'read_skill_file':
        skill, relative = str(values['skill']), str(values['path'])
        if skill not in loaded:
            raise ValueError('当前会话尚未加载这个 Skill。')
        return SkillToolExecution({**runtime.service.catalog.read(skill, relative), 'skill': skill})

    row = checkpoint.repository.info(checkpoint.run_id)
    account, conversation = row['account_id'], row['conversation_id']
    if name == 'read_work_file':
        return SkillToolExecution(runtime.service.read_work(
            account, conversation, checkpoint.run_id, values['path'], values['offset'], values['limit']))
    if name == 'write_work_file':
        return SkillToolExecution(runtime.service.write_work(
            account, conversation, checkpoint.run_id, values['path'], values['content']))
    if name == 'run_skill_command':
        return SkillToolExecution(runtime.start(
            checkpoint, [str(skill['name']) for skill in state['loaded_skills']],
            values['command'], values['seconds']))
    if name == 'poll_skill_command':
        return SkillToolExecution(runtime.poll(
            checkpoint, values['command_id'], values['offset'], values['wait_seconds']))
    if name == 'import_skill_source':
        return SkillToolExecution(import_source(
            values['source_id'], str(values['kind']), checkpoint, runtime))
    if name == 'publish_skill_file':
        return SkillToolExecution(runtime.service.publish(
            account, conversation, checkpoint.run_id, values['path'],
            uuid5(checkpoint.run_id, checkpoint.current_call or str(values['path'])),
        ))
    if name == 'view_work_image':
        result, messages = view_image(str(values['path']), checkpoint, runtime)
        return SkillToolExecution(result, image_messages=messages)
    if name == 'open_skill_preview':
        return SkillToolExecution(runtime.open_preview(
            checkpoint, values['project_path'], values['surface']))
    raise ValueError('未知 Skill 工具。')


def import_source(identifier: UUID, kind: str, checkpoint: RunCheckpoint,
                  runtime: SkillRuntime) -> dict[str, Any]:
    """把当前会话获准访问的附件或知识库原件复制到 workspace。"""
    row = checkpoint.repository.info(checkpoint.run_id)
    account, conversation = row['account_id'], row['conversation_id']
    if kind == 'attachment':
        source, filename = runtime.attachments.file(conversation, identifier, account)
    elif kind == 'knowledge':
        with runtime.service.database.connect() as connection:
            document = connection.execute(
                'SELECT d.filename FROM knowledge_documents d '
                'JOIN conversation_knowledge_bases s ON s.knowledge_base_id=d.knowledge_base_id '
                'JOIN knowledge_bases b ON b.id=d.knowledge_base_id '
                "WHERE d.id=%s AND s.conversation_id=%s AND b.user_id=%s "
                "AND NOT b.deleting AND d.status='ready'",
                (identifier, conversation, account),
            ).fetchone()
        if document is None:
            raise LookupError('文档不属于当前会话选定的知识库。')
        source = runtime.service.directory.parent / 'knowledge' / str(identifier) / 'original'
        filename = document['filename']
    else:
        raise ValueError('来源类型必须为 attachment 或 knowledge。')
    relative = f'inputs/{identifier}/{Path(filename).name}'
    root = runtime.service.workspace(account, conversation, create=True)
    with source.open('rb') as original, SafeFiles(root).file(relative, write=True) as destination:
        while chunk := original.read(1024 * 1024):
            destination.write(chunk)
    return {'source_id': str(identifier), 'filename': filename, 'path': relative}


def view_image(relative: str, checkpoint: RunCheckpoint,
               runtime: SkillRuntime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """校验当前 Run workspace 图片并返回视觉模型消息，不暴露宿主路径。"""
    row = checkpoint.repository.info(checkpoint.run_id)
    account, conversation = row['account_id'], row['conversation_id']
    filename = Path(relative).name
    workspace = runtime.service.workspace(account, conversation)
    with SafeFiles(workspace).file(relative) as original:
        data = original.read(20_000_001)
    if len(data) > 20_000_000:
        raise ValueError('预览图片超过 20 MB。')
    conversation_dir = runtime.service.conversation_directory(account, conversation)
    with NamedTemporaryFile(dir=conversation_dir, suffix=Path(filename).suffix) as temporary:
        temporary.write(data)
        temporary.flush()
        path = Path(temporary.name)
        metadata = validate_image(path, path.suffix.lower())
        image_part = image_url_part(path, metadata['mime'])
    message = {
        'id': f'image:{checkpoint.run_id}:{checkpoint.current_call}:{relative}',
        'role': 'user',
        'content': [
            {'type': 'text', 'text': f'当前 Run 实际页面预览：{relative}'},
            image_part,
        ],
    }
    return {'image_loaded': True, 'path': relative}, [message]
