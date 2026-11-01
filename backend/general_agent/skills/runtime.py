"""将 Skill 工具、异步命令和可信指令注入接到现有主智能体执行。"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from general_agent.agent.repository import RunCheckpoint
from general_agent.attachments.service import AttachmentService
from general_agent.infrastructure.settings import StoredSettings
from general_agent.infrastructure.tokens import TokenCounter
from general_agent.skills.sandbox import DockerSandbox
from general_agent.skills.service import SkillService
from general_agent.skills.files import SafeFiles
from general_agent.skills.tools import validate, skill_tools

LOGGER = logging.getLogger(__name__)


class SkillRuntime:
    """保存真实命令状态和稳定调用身份，图检查点负责模型与工具循环续接。"""

    def __init__(self, service: SkillService, attachments: AttachmentService) -> None:
        """绑定隔离执行和受保护来源读取，后台最多同时运行三个制作命令。"""
        self.service, self.attachments = service, attachments
        self.sandbox = DockerSandbox(service.directory.parent)
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='skill-command')
        self.events: dict[UUID, threading.Event] = {}
        self.lock = threading.Lock()
        self.closed = False

    def environment(self) -> dict[str, str]:
        """读取管理员单独保存的提供商变量，不继承应用环境和数据库凭据。"""
        path = self.service.directory.parent / 'skill-providers.json'
        return dict(json.loads(path.read_text())) if path.exists() else {}

    def recover(self) -> None:
        """重启中断旧命令并清理孤立任务目录，已保存工作文件等待显式续接。"""
        with self.service.database.connect() as connection:
            running = connection.execute("UPDATE skill_commands SET status='interrupted',finished_at=CURRENT_TIMESTAMP WHERE status='running' RETURNING task_id").fetchall()
            connection.execute("UPDATE skill_tasks SET status='interrupted' WHERE status='active'")
            valid = {str(row['id']) for row in connection.execute('SELECT id FROM skill_tasks').fetchall()}
        for row in running:
            self.sandbox.stop(row['task_id'])
        import shutil
        self.service.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in self.service.directory.iterdir():
            if path.is_dir() and path.name not in valid:
                self.sandbox.stop(UUID(path.name))
                shutil.rmtree(path)

    def close(self) -> None:
        """停机撤销后台命令并等待监测线程退出，不留下继续写入的制作进程。"""
        self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)

    def maintain(self) -> None:
        """定期清理删除会话的孤立容器与文件，并停止来源变化或长期闲置的任务。"""
        import shutil
        with self.service.database.connect() as connection:
            rows = connection.execute("SELECT t.*,EXISTS(SELECT 1 FROM skill_commands c WHERE c.task_id=t.id AND c.status='running') AS running FROM skill_tasks t").fetchall()
        valid = {str(row['id']) for row in rows}
        for path in self.service.directory.iterdir():
            if path.is_dir() and path.name not in valid:
                self.sandbox.stop(UUID(path.name))
                shutil.rmtree(path, ignore_errors=True)
        for row in rows:
            try:
                self.service.get(row['id'], row['account_id'], row['conversation_id'])
            except (ValueError, LookupError):
                self.sandbox.stop(row['id'])
                continue
            if not row['running'] and row['updated_at'] < datetime.now(timezone.utc) - timedelta(hours=2):
                self.sandbox.stop(row['id'])

    def start(self, task: UUID, checkpoint: RunCheckpoint, command: str, seconds: int) -> dict[str, Any]:
        """按原工具调用身份复用命令记录，避免检查点重放再次启动副作用。"""
        identifier = uuid4()
        answer_id = checkpoint.repository.info(checkpoint.run_id)['answer_attempt_id']
        call_key = str(checkpoint.run_id) + ':' + checkpoint.current_call if checkpoint.current_call else None
        with self.lock, self.service.database.connect() as connection:
            if call_key:
                previous = connection.execute('SELECT id,status FROM skill_commands WHERE answer_id=%s AND tool_call_key=%s', (answer_id, call_key)).fetchone()
                if previous:
                    return {'command_id': str(previous['id']), 'status': previous['status'], 'reused': True}
            if self.closed:
                raise ValueError('制作执行环境正在关闭。')
            count = connection.execute("SELECT count(*) AS n FROM skill_commands WHERE status='running'").fetchone()
            assert count is not None
            if count['n'] >= 3:
                raise ValueError('制作命令并发已满，请等待已有命令结束。')
            if connection.execute("SELECT id FROM skill_commands WHERE task_id=%s AND status='running'", (task,)).fetchone():
                raise ValueError('当前任务已有命令正在执行，请先轮询该命令。')
            connection.execute('INSERT INTO skill_commands(id,task_id,answer_id,command,deadline,tool_call_key) VALUES(%s,%s,%s,%s,%s,%s)',
                (identifier, task, answer_id, command,
                 datetime.now(timezone.utc) + timedelta(seconds=seconds), call_key))
            connection.execute("UPDATE skill_tasks SET status='active',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (task,))
        event = threading.Event()
        self.events[identifier] = event
        self.pool.submit(self.execute, identifier, task, checkpoint, command, seconds, event)
        return {'command_id': str(identifier), 'status': 'running'}

    def execute(self, identifier: UUID, task: UUID, checkpoint: RunCheckpoint, command: str,
                seconds: int, event: threading.Event) -> None:
        """命令输出留在宿主私有日志，持续复核回答与来源状态后发布退出码。"""
        directory = self.service.directory / str(task) / 'commands'
        status, code, detail = 'failed', None, None

        def guard() -> None:
            """应用关闭、用户停止、来源变化或执行过期时拒绝继续命令。"""
            if self.closed:
                raise ValueError('应用正在关闭。')
            checkpoint.guard()
            account = checkpoint.operation.account_id
            assert account is not None
            self.service.get(task, account, checkpoint.conversation_id)

        try:
            guard()
            directory.mkdir(exist_ok=True)
            account = checkpoint.operation.account_id
            assert account is not None
            row = self.service.get(task, account, checkpoint.conversation_id)
            self.service.prepare_library(task, row['skill'])
            code = self.sandbox.run(task, command, directory / f'{identifier}.log', guard, self.environment(), seconds, row['skill'])
            status = 'completed' if code == 0 else 'failed'
        except Exception as error:
            LOGGER.info('Skill command interrupted task=%s command=%s error_type=%s', task, identifier, type(error).__name__)
            status = 'interrupted'
            detail = str(error) if isinstance(error, (ValueError, LookupError)) else '制作命令运行失败，请检查隔离环境。'
        finally:
            try:
                with self.service.database.connect() as connection:
                    connection.execute('UPDATE skill_commands SET status=%s,exit_code=%s,error=%s,finished_at=CURRENT_TIMESTAMP WHERE id=%s', (status, code, detail, identifier))
            finally:
                event.set()

    def poll(self, task: UUID, identifier: UUID, offset: int, wait: int) -> dict[str, Any]:
        """短暂等待真实命令并分段读取日志，提供商密钥不进入模型上下文。"""
        with self.service.database.connect() as connection:
            row = connection.execute('SELECT status,exit_code,error FROM skill_commands WHERE id=%s AND task_id=%s', (identifier, task)).fetchone()
        if row is None:
            raise LookupError('命令不属于当前任务。')
        event = self.events.get(identifier)
        if row['status'] == 'running' and event:
            event.wait(wait)
            with self.service.database.connect() as connection:
                row = connection.execute('SELECT status,exit_code,error FROM skill_commands WHERE id=%s', (identifier,)).fetchone()
        assert row is not None
        if row.get('error') is None:
            row.pop('error', None)
        path = self.service.directory / str(task) / 'commands' / f'{identifier}.log'
        text = path.read_text(errors='replace') if path.exists() else ''
        for key, value in self.environment().items():
            if ('KEY' in key or 'TOKEN' in key or 'SECRET' in key) and value:
                text = text.replace(value, '[已隐藏凭据]')
        end = min(offset + 30000, len(text))
        return {**row, 'output': text[offset:end], 'next_offset': end, 'has_more': end < len(text)}


class SkillSession:
    """本次主执行仅激活一个制作任务，普通聊天始终只获得目录和任务摘要。"""

    def __init__(self, runtime: SkillRuntime, checkpoint: RunCheckpoint, counter: TokenCounter, settings: StoredSettings) -> None:
        """绑定真实执行身份，不接受模型传账号、会话或回答标识。"""
        self.runtime, self.checkpoint = runtime, checkpoint
        self.service = runtime.service
        assert checkpoint.operation.account_id is not None
        self.account, self.conversation = checkpoint.operation.account_id, checkpoint.conversation_id
        self.task: UUID | None = None
        self.images: list[dict[str, Any]] = []
        self.counter, self.settings = counter, settings
        self.request_messages: list[dict[str, Any]] = []
        self.request_tools: list[dict[str, Any]] = []
        self.injected_context = ''

    def snapshot(self) -> dict[str, Any]:
        """保存任务身份及有效指令摘要，恢复时重新核对安装文件。"""
        if self.task is None:
            return {}
        row = self.service.get(self.task, self.account, self.conversation)
        return {'task_id': str(self.task), 'skill': row['skill'], 'version': row['version'],
            'entry_digest': row['entry_digest'], 'instruction_files': row['instruction_files']}

    def restore(self, snapshot: dict[str, Any]) -> None:
        """还原固定版本任务和有效指令集合，版本不符时拒绝静默升级。"""
        if not snapshot.get('task_id'):
            return
        task = UUID(snapshot['task_id'])
        row = self.service.get(task, self.account, self.conversation)
        if any(snapshot.get(key, row[key]) != row[key] for key in ('skill', 'version', 'entry_digest', 'instruction_files')):
            raise ValueError('制作任务指令版本已变化，请重试。')
        self.service.instructions(task, self.account, self.conversation)
        self.task = task
        with self.service.database.connect() as connection:
            connection.execute("UPDATE skill_tasks SET status='active',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (task,))

    def context(self, replacement: str | None = None) -> list[str]:
        """按每次请求重建可信目录和当前活动指令，历史任务只以资料摘要提供。"""
        result = [self.service.catalog.prompt()]
        if self.task:
            result.append(
            'Skill 运行说明：任务文件路径均相对 /workspace。使用 read_work_file/write_work_file；'
            '按上游顺序执行脚本，命令异步启动后必须轮询真实退出码。'
            '服务器没有桌面浏览器，启动上游预览使用 --no-browser，随后 open_skill_preview。'
            '默认 Confirm UI 启动后用 open_skill_preview(surface="confirmation") 返回真实页面；'
            '需要等待用户时结束当前回答；状态由后端自动保存，用户回来后读取实际确认回执。'
            '模板库是本任务独立可写副本，路径为 SKILL_DIR/templates。'
            '模板索引与模板规范是参考资料，read_skill_file 使用 instruction=false。'
            '用户确认环节按 Skill 原文要求处理。'
            '需要外部图片或语音服务时使用已配置环境，不猜测配置。'
            '导出并检查后 publish_skill_file 才能提供真实下载链接。'
            '工作状态由后端自动持久化，上下文由系统按预算压缩，不需要保存状态工具。'
            )
        if self.task:
            result.append(replacement if replacement is not None else self.service.instructions(self.task, self.account, self.conversation))
        if replacement is None:
            self.injected_context = '\n\n'.join(result)
        return result

    def check_instructions(self, instruction: str) -> None:
        """提交指令集合前检查完整模型请求预算，超限不保存有毒的活动上下文。"""
        if not self.request_messages:
            raise ValueError('尚无模型请求上下文，不能加载指令。')
        system = str(self.request_messages[0]['content'])
        base = system.removesuffix('\n\n' + self.injected_context)
        prospective = '\n\n'.join(self.context(replacement=instruction))
        messages = [{'role': 'system', 'content': base + '\n\n' + prospective}, *self.request_messages[1:]]
        tools = {tool['function']['name']: tool for tool in [*self.request_tools, *skill_tools()]}
        if self.counter.available(self.settings, messages, list(tools.values())) < 2048:
            raise ValueError('完整 Skill 指令超过当前模型上下文预算；请保留当前阶段必要文件或调整模型窗口后重试。')

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """验证模型参数后执行 Skill 工具，所有文件和命令均要求本执行已显式激活任务。"""
        values = validate(name, arguments)
        if name == 'load_skill':
            row = self.service.activate(self.account, self.conversation, values['name'], values['task_id'],
                uuid5(self.checkpoint.run_id, self.checkpoint.current_call) if self.checkpoint.current_call else None)
            previous = self.task
            self.task = row['id']
            try:
                self.check_instructions(self.service.instructions(self.task, self.account, self.conversation))
            except ValueError:
                self.task = previous
                raise
            with self.service.database.connect() as connection:
                connection.execute("UPDATE agents SET model_limit=384,deadline=GREATEST(deadline,created_at+INTERVAL '1 hour') WHERE answer_attempt_id=%s AND parent_agent_id IS NULL", (self.checkpoint.repository.info(self.checkpoint.run_id)['answer_attempt_id'],))
                connection.execute("UPDATE agent_runs SET deadline=CURRENT_TIMESTAMP+INTERVAL '1 hour' WHERE id=%s", (self.checkpoint.run_id,))
            return {'task_id': str(self.task), 'skill': row['skill'], 'version': row['version'],
                'stage': row['stage'], 'status': row['status'], 'skill_dir': f'/opt/skills/{row["skill"]}',
                'previous_instruction_paths': row.get('previous_instruction_paths', []),
                'workspace': '/workspace', 'instructions_loaded': True}
        task = values.pop('task_id')
        if task != self.task:
            raise ValueError('请先用 load_skill 显式激活这个任务。')
        self.service.get(task, self.account, self.conversation)
        if name == 'read_skill_file':
            loaded = self.service.load_file(task, self.account, self.conversation, values['path'], values['instruction'], self.check_instructions)
            return ({'path': loaded['absolute_path'], 'instructions_loaded': True}
                if values['instruction'] else loaded)
        if name == 'set_skill_context':
            row = self.service.get(task, self.account, self.conversation)
            paths = {'SKILL.md', *values['paths']}
            if not paths.issubset(row['instruction_files']):
                raise ValueError('只能保留已经完整读取的指令文件。')
            from psycopg.types.json import Jsonb
            files = {path: row['instruction_files'][path] for path in paths}
            with self.service.database.connect() as connection:
                connection.execute('UPDATE skill_tasks SET instruction_files=%s WHERE id=%s', (Jsonb(files), task))
            return {'retained': sorted(paths)}
        if name == 'read_work_file':
            return self.service.read(task, self.account, self.conversation, **{'relative': values['path'], 'offset': values['offset'], 'limit': values['limit']})
        if name == 'write_work_file':
            return self.service.write(task, self.account, self.conversation, values['path'], values['content'])
        if name == 'run_skill_command':
            return self.runtime.start(task, self.checkpoint, values['command'], values['seconds'])
        if name == 'poll_skill_command':
            return self.runtime.poll(task, values['command_id'], values['offset'], values['wait_seconds'])
        if name == 'import_skill_source':
            return self.import_source(task, values['source_id'], values['kind'])
        if name == 'publish_skill_file':
            return self.service.publish(task, self.account, self.conversation,
                self.checkpoint.repository.info(self.checkpoint.run_id)['answer_attempt_id'], values['path'],
                uuid5(self.checkpoint.run_id, self.checkpoint.current_call) if self.checkpoint.current_call else None)
        if name == 'view_work_image':
            from general_agent.attachments.images import validate_image
            from tempfile import NamedTemporaryFile
            filename = Path(values['path']).name
            with SafeFiles(self.service.directory / str(task) / 'workspace').file(values['path']) as original:
                data = original.read(20_000_001)
            if len(data) > 20_000_000:
                raise ValueError('预览图片超过 20 MB。')
            with NamedTemporaryFile(dir=self.service.directory / str(task), suffix=Path(filename).suffix) as temporary:
                temporary.write(data)
                temporary.flush()
                path = Path(temporary.name)
                metadata = validate_image(path, path.suffix.lower())
                remote = self.runtime.attachments.files.upload(path, filename, metadata['mime'])
            self.images.append({'role': 'user', 'content': [{'type': 'text', 'text': f'制作任务实际页面预览：{values["path"]}'}, {'type': 'file', 'file_id': remote['id']}]})
            return {'image_loaded': True, 'path': values['path']}
        if name == 'open_skill_preview':
            name = '.confirm_ui.lock' if values['surface'] == 'confirmation' else 'live_preview/lock.json'
            relative = str(Path(values['project_path']) / name)
            lock = json.loads(self.service.read(task, self.account, self.conversation, relative)['content'])
            port = int(lock['port'])
            if not 1024 <= port <= 65535:
                raise ValueError('预览端口无效。')
            return {'url': f'/api/conversations/{self.conversation}/skill-tasks/{task}/preview/{port}/'}
        raise ValueError('未知 Skill 工具。')

    def import_source(self, task: UUID, identifier: UUID, kind: str) -> dict[str, Any]:
        """按会话附件或知识库绑定重新授权，复制原件到保留真实扩展名的任务路径。"""
        import shutil
        if kind == 'attachment':
            source, filename = self.runtime.attachments.file(self.conversation, identifier, self.account)
        elif kind == 'knowledge':
            with self.service.database.connect() as connection:
                row = connection.execute("SELECT d.filename FROM knowledge_documents d JOIN conversation_knowledge_bases s ON s.knowledge_base_id=d.knowledge_base_id JOIN knowledge_bases b ON b.id=d.knowledge_base_id WHERE d.id=%s AND s.conversation_id=%s AND b.user_id=%s AND NOT b.deleting AND d.status='ready'", (identifier, self.conversation, self.account)).fetchone()
            if row is None:
                raise LookupError('文档不属于当前会话选定的知识库。')
            source = self.service.directory.parent / 'knowledge' / str(identifier) / 'original'
            filename = row['filename']
        else:
            raise ValueError('来源类型必须为 attachment 或 knowledge。')
        relative = f'inputs/{identifier}/{Path(filename).name}'
        with source.open('rb') as original, SafeFiles(self.service.directory / str(task) / 'workspace').file(relative, write=True) as destination:
            shutil.copyfileobj(original, destination)
        return {'source_id': str(identifier), 'filename': filename, 'path': '/workspace/' + relative}
