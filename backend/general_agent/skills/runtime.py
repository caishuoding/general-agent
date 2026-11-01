"""将无状态 Skill 工具、异步命令和当前 Run 工作区接到主智能体。"""

import json
import logging
import secrets
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from general_agent.agent.repository import RunCheckpoint
from general_agent.attachments.service import AttachmentService
from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.skills.files import SafeFiles
from general_agent.skills.sandbox import DockerSandbox
from general_agent.skills.service import SkillService
from general_agent.skills.tools import validate, skill_tools

LOGGER = logging.getLogger(__name__)


class SkillRuntime:
    """保存真实命令状态和稳定调用身份，不创建 Skill 任务或第二套上下文。"""

    def __init__(self, service: SkillService, attachments: AttachmentService) -> None:
        """绑定按 Run 文件服务、隔离执行和受保护来源读取。"""
        self.service, self.attachments = service, attachments
        self.sandbox = DockerSandbox(service.directory)
        self.pool = ThreadPoolExecutor(max_workers=LIMITS.skill_command_concurrency, thread_name_prefix='skill-command')
        self.events: dict[UUID, threading.Event] = {}
        self.lock = threading.Lock()
        self.closed = False

    def environment(self) -> dict[str, str]:
        """读取管理员单独保存的提供商变量，不继承应用环境和数据库凭据。"""
        path = self.service.directory.parent / 'skill-providers.json'
        return dict(json.loads(path.read_text())) if path.exists() else {}

    def recover(self) -> None:
        """重启时把未完成命令标为中断并停止对应 Run 容器，保留生成文件。"""
        with self.service.database.connect() as connection:
            running = connection.execute(
                "UPDATE skill_commands SET status='interrupted',finished_at=CURRENT_TIMESTAMP "
                "WHERE status='running' RETURNING run_id"
            ).fetchall()
        self.stop_runs(running)

    def stop_runs(self, rows: list[dict[str, Any]]) -> None:
        """停止一组已失效命令所属的 Run 容器。"""
        for row in rows:
            self.sandbox.stop(row['run_id'])

    def close(self) -> None:
        """停机时拒绝新命令、等待运行任务退出，并关闭被取消的排队命令记录。"""
        self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.service.database.connect() as connection:
            rows = connection.execute(
                "UPDATE skill_commands SET status='interrupted',finished_at=CURRENT_TIMESTAMP "
                "WHERE status='running' RETURNING run_id"
            ).fetchall()
        self.stop_runs(rows)

    def maintain(self) -> None:
        """停止超过两小时没有运行命令的旧 Run 容器，保留文件供后续下载。"""
        with self.service.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT run_id FROM skill_commands c WHERE c.status<>'running' "
                "AND c.created_at<CURRENT_TIMESTAMP-INTERVAL '2 hours' "
                "AND NOT EXISTS (SELECT 1 FROM skill_commands active "
                "WHERE active.run_id=c.run_id AND active.status='running')"
            ).fetchall()
        self.stop_runs(rows)

    def delete_conversation(self, connection: Any, account: UUID, conversation: UUID) -> None:
        """停止会话全部 Run 容器并删除本会话的生成目录。"""
        rows = connection.execute(
            'SELECT DISTINCT r.id AS run_id FROM agent_runs r '
            'JOIN agents a ON a.id=r.agent_id WHERE a.conversation_id=%s',
            (conversation,),
        ).fetchall()
        self.stop_runs(rows)
        path = self.service.directory / str(account) / str(conversation)
        if path.exists():
            shutil.rmtree(path)

    def start(self, checkpoint: RunCheckpoint, skills: list[str], command: str,
              seconds: int) -> dict[str, Any]:
        """按当前 Run 和工具调用身份启动一条幂等异步命令。"""
        if not 1 <= seconds <= LIMITS.skill_command_seconds:
            raise ValueError('制作命令执行时限无效。')
        checkpoint.guard()
        row = checkpoint.repository.info(checkpoint.run_id)
        account, conversation = row['account_id'], row['conversation_id']
        self.service.run(account, conversation, checkpoint.run_id)
        identifier = uuid5(checkpoint.run_id, checkpoint.current_call or command)
        call_key = checkpoint.current_call
        with self.lock, self.service.database.connect() as connection:
            if call_key:
                previous = connection.execute(
                    'SELECT id,status FROM skill_commands WHERE run_id=%s AND tool_call_key=%s',
                    (checkpoint.run_id, call_key),
                ).fetchone()
                if previous:
                    return {'command_id': str(previous['id']), 'status': previous['status'], 'reused': True}
            if self.closed:
                raise ValueError('制作执行环境正在关闭。')
            count = connection.execute("SELECT count(*) AS n FROM skill_commands WHERE status='running'").fetchone()
            assert count is not None
            if count['n'] >= LIMITS.skill_command_concurrency:
                raise ValueError('制作命令并发已满，请等待已有命令结束。')
            if connection.execute(
                "SELECT id FROM skill_commands WHERE run_id=%s AND status='running'",
                (checkpoint.run_id,),
            ).fetchone():
                raise ValueError('当前 Run 已有命令正在执行，请先轮询该命令。')
            connection.execute(
                'INSERT INTO skill_commands(id,account_id,conversation_id,run_id,command,'
                'deadline,tool_call_key) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (identifier, account, conversation, checkpoint.run_id, command,
                 datetime.now(timezone.utc) + timedelta(seconds=seconds), call_key),
            )
        event = threading.Event()
        self.events[identifier] = event
        self.pool.submit(self.execute, identifier, checkpoint, skills, command, seconds, event)
        return {'command_id': str(identifier), 'status': 'running'}

    def execute(self, identifier: UUID, checkpoint: RunCheckpoint, skills: list[str],
                command: str, seconds: int, event: threading.Event) -> None:
        """在隔离容器执行命令，持续核验回答状态并把真实退出码写入数据库。"""
        status, code, detail = 'failed', None, None

        def guard() -> None:
            """应用关闭、回答停止或账号会话失效时拒绝继续命令。"""
            if self.closed:
                raise ValueError('应用正在关闭。')
            checkpoint.guard()
            row = checkpoint.repository.info(checkpoint.run_id)
            self.service.run(row['account_id'], row['conversation_id'], checkpoint.run_id)

        try:
            guard()
            row = checkpoint.repository.info(checkpoint.run_id)
            account, conversation = row['account_id'], row['conversation_id']
            workspace = self.service.workspace(account, conversation, create=True)
            libraries = self.service.prepare_libraries(account, conversation, skills)
            command_dir = self.service.command_directory(account, conversation, checkpoint.run_id)
            output = command_dir / f'{identifier}.log'
            code = self.sandbox.run(checkpoint.run_id, command, output, guard, self.environment(),
                                    seconds, workspace, libraries)
            status = 'completed' if code == 0 else 'failed'
        except Exception as error:
            LOGGER.info('Skill command interrupted run=%s command=%s error_type=%s',
                        checkpoint.run_id, identifier, type(error).__name__)
            status = 'interrupted'
            detail = str(error) if isinstance(error, (ValueError, LookupError)) else '制作命令运行失败，请检查隔离环境。'
        finally:
            try:
                with self.service.database.connect() as connection:
                    connection.execute(
                        'UPDATE skill_commands SET status=%s,exit_code=%s,error=%s,finished_at=CURRENT_TIMESTAMP '
                        'WHERE id=%s',
                        (status, code, detail, identifier),
                    )
            finally:
                event.set()

    def poll(self, checkpoint: RunCheckpoint, identifier: UUID, offset: int, wait: int) -> dict[str, Any]:
        """等待并读取当前 Run 命令状态，对外只返回有界且脱敏的日志。"""
        checkpoint.guard()
        row = checkpoint.repository.info(checkpoint.run_id)
        account, conversation = row['account_id'], row['conversation_id']
        with self.service.database.connect() as connection:
            command = connection.execute(
                'SELECT status,exit_code,error FROM skill_commands '
                'WHERE id=%s AND account_id=%s AND conversation_id=%s AND run_id=%s',
                (identifier, account, conversation, checkpoint.run_id),
            ).fetchone()
        if command is None:
            raise LookupError('命令不属于当前 Run。')
        event = self.events.get(identifier)
        if command['status'] == 'running' and event:
            event.wait(wait)
            with self.service.database.connect() as connection:
                command = connection.execute(
                    'SELECT status,exit_code,error FROM skill_commands WHERE id=%s',
                    (identifier,),
                ).fetchone()
        assert command is not None
        if command.get('error') is None:
            command.pop('error', None)
        path = self.service.command_directory(account, conversation, checkpoint.run_id) / f'{identifier}.log'
        text = path.read_text(errors='replace') if path.exists() else ''
        for key, value in self.environment().items():
            if ('KEY' in key or 'TOKEN' in key or 'SECRET' in key) and value:
                text = text.replace(value, '[已隐藏凭据]')
        end = min(offset + 30000, len(text))
        return {**command, 'output': text[offset:end], 'next_offset': end, 'has_more': end < len(text)}

    def open_preview(self, checkpoint: RunCheckpoint, project_path: str, surface: str) -> dict[str, Any]:
        """读取当前 Run 项目的预览锁并返回一小时能力令牌页面。"""
        checkpoint.guard()
        row = checkpoint.repository.info(checkpoint.run_id)
        account, conversation = row['account_id'], row['conversation_id']
        lock_name = '.confirm_ui.lock' if surface == 'confirmation' else 'live_preview/lock.json'
        relative = str(Path(project_path) / lock_name)
        workspace = self.service.workspace(account, conversation)
        with SafeFiles(workspace).file(relative) as source:
            lock = json.loads(source.read(1_000_001).decode('utf-8'))
        port = int(lock['port'])
        if not 1024 <= port <= 65535:
            raise ValueError('预览端口无效。')
        token = secrets.token_urlsafe(32)
        import hashlib
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.service.database.connect() as connection:
            connection.execute(
                'INSERT INTO skill_previews(token_digest,account_id,conversation_id,run_id,port,expires_at) '
                'VALUES(%s,%s,%s,%s,%s,%s)',
                (digest, account, conversation, checkpoint.run_id, port,
                 datetime.now(timezone.utc) + timedelta(hours=1)),
            )
        return {'url': f'/skill-previews/{token}/'}


class SkillSession:
    """当前 Agent Run 的 Skill 名称集合与工具适配，不持久化任务。"""

    def __init__(self, runtime: SkillRuntime, checkpoint: RunCheckpoint, used_skills: list[str]) -> None:
        """绑定真实执行身份并初始化会话中已使用的 Skill 名称。"""
        self.runtime, self.checkpoint = runtime, checkpoint
        self.service = runtime.service
        account = checkpoint.operation.account_id
        assert account is not None
        self.account: UUID = account
        self.conversation = checkpoint.conversation_id
        self.used_skills = list(dict.fromkeys(used_skills))
        self.images: list[dict[str, Any]] = []

    def current_skills(self) -> list[str]:
        """返回当前 Run 可用 Skill 名称，供 State 与工具开放条件使用。"""
        return list(self.used_skills)

    def context(self) -> list[str]:
        """每轮提供完整目录和当前 Run 的相对路径约定，不重复注入 Skill 正文。"""
        result = [self.service.catalog.prompt()]
        if self.used_skills:
            roots = '\n'.join(f'- {name}: /opt/skills/{name}' for name in self.used_skills)
            result.append(
                'Skill 运行说明：所有 work 文件路径都相对于 /workspace；'
                'SKILL_DIR 指向当前 Skill 容器目录。\n' + roots + '\n'
                '工作状态直接保存在 Agent messages、workspace 和生成文件中。'
                '命令异步启动后必须轮询真实退出码。'
                '需要查看渲染结果时使用 view_work_image；'
                '上游要求用户交互时使用 open_skill_preview；'
                '导出并检查后使用 publish_skill_file 提供真实下载。'
            )
        return result

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """校验工具参数并在当前 Run 身份下执行，不接受模型提供任务或目录标识。"""
        values = validate(name, arguments)
        self.checkpoint.guard()
        if name == 'load_skill':
            skill = str(values['name'])
            item = self.service.catalog.read(skill, 'SKILL.md')
            if skill not in self.used_skills:
                self.used_skills.append(skill)
            with self.service.database.connect() as connection:
                self.service.remember_skills(connection, self.conversation, [skill])
            return {'name': skill, 'version': item['version'], 'skill_dir': f'/opt/skills/{skill}',
                    'content': item['content'], 'digest': item['file_digest']}
        if name == 'read_skill_file':
            skill, relative = str(values['skill']), str(values['path'])
            if skill not in self.used_skills:
                raise ValueError('当前会话尚未使用这个 Skill。')
            item = self.service.catalog.read(skill, relative)
            return {'skill': skill, 'path': item['path'], 'absolute_path': item['absolute_path'],
                    'content': item['content'], 'digest': item['file_digest']}
        if not self.used_skills:
            raise ValueError('当前 Run 尚未加载 Skill。')
        row = self.checkpoint.repository.info(self.checkpoint.run_id)
        account, conversation = row['account_id'], row['conversation_id']
        if name == 'read_work_file':
            return self.service.read_work(account, conversation, self.checkpoint.run_id,
                                          values['path'], values['offset'], values['limit'])
        if name == 'write_work_file':
            return self.service.write_work(account, conversation, self.checkpoint.run_id,
                                           values['path'], values['content'])
        if name == 'run_skill_command':
            return self.runtime.start(self.checkpoint, self.current_skills(), values['command'], values['seconds'])
        if name == 'poll_skill_command':
            return self.runtime.poll(self.checkpoint, values['command_id'], values['offset'], values['wait_seconds'])
        if name == 'import_skill_source':
            return self.import_source(values['source_id'], values['kind'])
        if name == 'publish_skill_file':
            return self.service.publish(account, conversation, self.checkpoint.run_id, values['path'],
                                        uuid5(self.checkpoint.run_id, self.checkpoint.current_call or values['path']))
        if name == 'view_work_image':
            return self.view_image(values['path'])
        if name == 'open_skill_preview':
            return self.runtime.open_preview(self.checkpoint, values['project_path'], values['surface'])
        raise ValueError('未知 Skill 工具。')

    def import_source(self, identifier: UUID, kind: str) -> dict[str, Any]:
        """按会话来源授权把原件复制到当前会话 workspace 的相对输入路径。"""
        if kind == 'attachment':
            source, filename = self.runtime.attachments.file(self.conversation, identifier, self.account)
        elif kind == 'knowledge':
            with self.service.database.connect() as connection:
                row = connection.execute(
                    'SELECT d.filename FROM knowledge_documents d '
                    'JOIN conversation_knowledge_bases s ON s.knowledge_base_id=d.knowledge_base_id '
                    'JOIN knowledge_bases b ON b.id=d.knowledge_base_id '
                    "WHERE d.id=%s AND s.conversation_id=%s AND b.user_id=%s "
                    "AND NOT b.deleting AND d.status='ready'",
                    (identifier, self.conversation, self.account),
                ).fetchone()
            if row is None:
                raise LookupError('文档不属于当前会话选定的知识库。')
            source = self.service.directory.parent / 'knowledge' / str(identifier) / 'original'
            filename = row['filename']
        else:
            raise ValueError('来源类型必须为 attachment 或 knowledge。')
        relative = f'inputs/{identifier}/{Path(filename).name}'
        root = self.service.workspace(self.account, self.conversation, create=True)
        with source.open('rb') as original, SafeFiles(root).file(relative, write=True) as destination:
            while chunk := original.read(1024 * 1024):
                destination.write(chunk)
        return {'source_id': str(identifier), 'filename': filename, 'path': relative}

    def view_image(self, relative: str) -> dict[str, Any]:
        """把当前 Run 中已校验的 PNG/JPEG 作为模型视觉材料加载。"""
        from general_agent.attachments.images import validate_image
        from tempfile import NamedTemporaryFile
        filename = Path(relative).name
        workspace = self.service.workspace(self.account, self.conversation)
        with SafeFiles(workspace).file(relative) as original:
            data = original.read(20_000_001)
        if len(data) > 20_000_000:
            raise ValueError('预览图片超过 20 MB。')
        conversation_dir = self.service.conversation_directory(self.account, self.conversation)
        with NamedTemporaryFile(dir=conversation_dir, suffix=Path(filename).suffix) as temporary:
            temporary.write(data)
            temporary.flush()
            path = Path(temporary.name)
            metadata = validate_image(path, path.suffix.lower())
            remote = self.runtime.attachments.files.upload(path, filename, metadata['mime'])
        self.images.append({'role': 'user', 'content': [
            {'type': 'text', 'text': f'当前 Run 实际页面预览：{relative}'},
            {'type': 'file', 'file_id': remote['id']},
        ]})
        return {'image_loaded': True, 'path': relative}
