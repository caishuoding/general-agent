"""以账号与会话为授权单位保存 Skill 制作任务和可续接的工作文件。"""

import hashlib
import json
import mimetypes
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.agent.repository import AgentRepository
from general_agent.infrastructure.database import Database
from general_agent.skills.catalog import SkillCatalog
from general_agent.skills.files import SafeFiles


class SkillService:
    """集中任务身份、版本、可信指令和文件授权，调用者不能指定磁盘根目录。"""

    def __init__(self, database: Database, directory: Path, catalog: SkillCatalog) -> None:
        """绑定业务存储和受信任目录；任务文件不与应用配置混放。"""
        self.database, self.catalog = database, catalog
        self.directory = directory.resolve() / 'skill-tasks'
        self.repository = AgentRepository(database)

    def list(self, account: UUID, conversation: UUID) -> list[dict[str, Any]]:
        """返回本会话任务摘要，普通聊天不自动激活旧任务的工作流。"""
        self.conversation(account, conversation)
        with self.database.connect() as connection:
            return connection.execute('SELECT id,skill,version,status,updated_at FROM skill_tasks WHERE account_id=%s AND conversation_id=%s ORDER BY updated_at DESC LIMIT 30', (account, conversation)).fetchall()

    def conversation(self, account: UUID, conversation: UUID) -> None:
        """每次访问核对所属账号和未删除会话，其他账号一律视为不存在。"""
        with self.database.connect() as connection:
            if not connection.execute('SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting', (conversation, account)).fetchone():
                raise LookupError('会话不存在。')

    def get(self, task: UUID, account: UUID, conversation: UUID) -> dict[str, Any]:
        """读取任务并复核来源，撤销后删除复制资料及产物，不允许旧任务继续外发。"""
        self.conversation(account, conversation)
        with self.database.connect() as connection:
            row = connection.execute('SELECT * FROM skill_tasks WHERE id=%s AND account_id=%s AND conversation_id=%s', (task, account, conversation)).fetchone()
        if row is None:
            raise LookupError('制作任务不存在。')
        if row['status'] == 'revoked':
            raise ValueError('制作任务的来源已撤销，请重新创建任务。')
        if row['source_snapshot'] != self.repository.snapshot(conversation):
            shutil.rmtree(self.directory / str(task), ignore_errors=True)
            with self.database.connect() as connection:
                connection.execute("UPDATE skill_tasks SET status='revoked',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (task,))
            raise ValueError('制作任务资料范围已变化，旧工作文件已清理，请重新创建任务。')
        return row

    def activate(self, account: UUID, conversation: UUID, skill: str, task: UUID | None, creation_id: UUID | None = None) -> dict[str, Any]:
        """新建或续接固定版本任务，稳定创建身份使重放不产生重复工作目录。"""
        self.conversation(account, conversation)
        entry = self.catalog.read(skill, 'SKILL.md')
        if task is not None:
            row = self.get(task, account, conversation)
            if row['skill'] != skill or row['entry_digest'] != entry['digest'] or row['version'] != entry['version']:
                raise ValueError('任务 Skill 版本与安装版本不一致，不能静默升级旧任务。')
            row['previous_instruction_paths'] = list(row['instruction_files'])
            row['instruction_files'] = {'SKILL.md': entry['file_digest']}
            with self.database.connect() as connection:
                connection.execute("UPDATE skill_tasks SET status='active',updated_at=CURRENT_TIMESTAMP,instruction_files=%s WHERE id=%s", (Jsonb(row['instruction_files']), task))
            row['status'] = 'active'
            return row
        identifier = creation_id or uuid4()
        with self.database.connect() as connection:
            existing = connection.execute('SELECT id FROM skill_tasks WHERE id=%s AND account_id=%s AND conversation_id=%s', (identifier, account, conversation)).fetchone()
        if existing:
            (self.directory / str(identifier) / 'workspace').mkdir(parents=True, mode=0o700, exist_ok=True)
            return self.get(identifier, account, conversation)
        snapshot = self.repository.snapshot(conversation)
        files = {'SKILL.md': entry['file_digest']}
        with self.database.connect() as connection:
            created = connection.execute('INSERT INTO skill_tasks(id,account_id,conversation_id,skill,version,entry_digest,instruction_files,source_snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                (identifier, account, conversation, skill, entry['version'], entry['digest'], Jsonb(files), Jsonb(snapshot))).fetchone()
        assert created is not None
        (self.directory / str(identifier) / 'workspace').mkdir(parents=True, mode=0o700)
        return created

    def prepare_library(self, task: UUID, skill: str) -> Path | None:
        """按任务复制模板库，允许上游注册模板且不改写安装目录或其他用户资源。"""
        source = self.catalog.directory / skill / 'templates'
        if not source.is_dir():
            return None
        destination = self.directory / str(task) / 'library'
        if not destination.exists():
            shutil.copytree(source, destination)
        return destination

    def load_file(self, task: UUID, account: UUID, conversation: UUID, path: str, instruction: bool,
                  check: Callable[[str], None] | None = None) -> dict[str, Any]:
        """完整读取引用文件，可信 Markdown 预检通过才保存，任务模板始终作为参考资料。"""
        row = self.get(task, account, conversation)
        if Path(path).parts and Path(path).parts[0] == 'templates':
            library = self.prepare_library(task, row['skill'])
            if instruction:
                raise ValueError('任务模板库是可修改参考资料，请使用 instruction=false 读取。')
            if library is None:
                raise ValueError('该 Skill 没有模板库。')
            data = SafeFiles(library).read(str(Path(path).relative_to('templates')), 0, 2_000_000)
            if data.get('next_offset') is not None:
                raise ValueError('模板资料过大，请按范围读取。')
            return {'path': path, 'absolute_path': f'/opt/skills/{row["skill"]}/{path}', **data}
        item = self.catalog.read(row['skill'], path)
        if instruction:
            if Path(path).suffix.lower() != '.md':
                raise ValueError('只有已安装的 Markdown 文档可以作为 Skill 指令加载。')
            files = {**row['instruction_files'], path: item['file_digest']}
            if check is not None:
                check(self.instructions(task, account, conversation, files))
            with self.database.connect() as connection:
                connection.execute('UPDATE skill_tasks SET instruction_files=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s', (Jsonb(files), task))
        return item

    def instructions(self, task: UUID, account: UUID, conversation: UUID, files: dict[str, str] | None = None) -> str:
        """重读活动或候选指令集合并验证摘要；候选集合只用于预算预检，不写入状态。"""
        row = self.get(task, account, conversation)
        parts = [f'已激活 Skill {row["skill"]}，版本 {row["version"]}。SKILL_DIR=/opt/skills/{row["skill"]}；工作目录=/workspace；任务 ID={task}。']
        for path, digest in (files if files is not None else row['instruction_files']).items():
            item = self.catalog.read(row['skill'], path)
            if item['file_digest'] != digest:
                raise ValueError('已激活 Skill 文件发生变化，请恢复固定版本后继续。')
            parts.append(f'可信 Skill 指令：{item["absolute_path"]}\n{item["content"]}')
        return '\n\n'.join(parts)

    def read(self, task: UUID, account: UUID, conversation: UUID, relative: str, offset: int = 0, limit: int = 20000) -> dict[str, Any]:
        """按字符范围读取任务文本或有界列出目录，明确续读位置且不伪称完整。"""
        self.get(task, account, conversation)
        return SafeFiles(self.directory / str(task) / 'workspace').read(relative, offset, limit)

    def write(self, task: UUID, account: UUID, conversation: UUID, relative: str, content: str) -> dict[str, Any]:
        """在受限工作目录保存完整 UTF-8 文本，不解释内容中的指令。"""
        self.get(task, account, conversation)
        if len(content.encode()) > 2_000_000:
            raise ValueError('单次写入超过两百万字节。')
        with SafeFiles(self.directory / str(task) / 'workspace').file(relative, write=True) as destination:
            destination.write(content.encode('utf-8'))
        return {'path': '/workspace/' + relative, 'bytes': len(content.encode('utf-8'))}

    def publish(self, task: UUID, account: UUID, conversation: UUID, answer: UUID, relative: str, artifact_id: UUID | None = None) -> dict[str, Any]:
        """按稳定发布身份保存不可变副本，检查点重放复用原交付且不覆盖内容。"""
        self.get(task, account, conversation)
        identifier = artifact_id or uuid4()
        with self.database.connect() as connection:
            existing = connection.execute('SELECT filename FROM skill_artifacts WHERE id=%s AND task_id=%s AND answer_id=%s', (identifier, task, answer)).fetchone()
        if existing:
            return {'artifact_id': str(identifier), 'filename': existing['filename'],
                'url': f'/api/conversations/{conversation}/skill-artifacts/{identifier}/file'}
        storage = self.directory / str(task) / 'artifacts'
        storage.mkdir(exist_ok=True)
        destination = storage / str(identifier)
        if not destination.exists():
            with SafeFiles(self.directory / str(task) / 'workspace').file(relative) as source:
                data = source.read(200_000_001)
            if len(data) > 200_000_000:
                raise ValueError('产物超过 200 MB。')
            temporary = storage / (str(identifier) + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        filename = Path(relative).name
        mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        with self.database.connect() as connection:
            connection.execute('INSERT INTO skill_artifacts(id,task_id,answer_id,filename,digest,media_type) VALUES(%s,%s,%s,%s,%s,%s)', (identifier, task, answer, filename, digest, mime))
        return {'artifact_id': str(identifier), 'filename': filename,
            'url': f'/api/conversations/{conversation}/skill-artifacts/{identifier}/file'}

    def artifact(self, artifact: UUID, account: UUID, conversation: UUID) -> tuple[Path, dict[str, Any]]:
        """下载前重新验证账号、会话、来源及产物摘要。"""
        with self.database.connect() as connection:
            row = connection.execute('SELECT a.* FROM skill_artifacts a JOIN skill_tasks t ON t.id=a.task_id WHERE a.id=%s AND t.account_id=%s AND t.conversation_id=%s', (artifact, account, conversation)).fetchone()
        if row is None:
            raise LookupError('生成文件不存在。')
        self.get(row['task_id'], account, conversation)
        path = self.directory / str(row['task_id']) / 'artifacts' / str(artifact)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['digest']:
            raise LookupError('生成文件已丢失或变化。')
        return path, row

    def runtime_state_for_answer(self, connection: Any, answer_id: UUID) -> dict[str, Any]:
        """读取主执行保存的 Skill 运行快照，供恢复和终态迁移共同使用。"""
        saved = connection.execute(
            "SELECT s.payload FROM agent_messages s JOIN agent_runs r ON r.id=s.run_id "
            "JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=%s AND "
            "a.parent_agent_id IS NULL AND s.step_key='runtime:skill'",
            (answer_id,),
        ).fetchone()
        payload = saved.get('payload') if isinstance(saved, dict) else None
        return dict(payload) if isinstance(payload, dict) else {}

    def settle_answer(self, connection: Any, answer_id: UUID, account: UUID,
                      conversation: UUID, status: str, runtime_state: dict[str, Any]) -> None:
        """按优先使用当前 State 的规则把活动任务同步为回答终态。"""
        task_id = runtime_state.get('task_id') if isinstance(runtime_state, dict) else None
        if not task_id:
            task_id = self.runtime_state_for_answer(connection, answer_id).get('task_id')
        if not isinstance(task_id, str):
            return
        connection.execute(
            "UPDATE skill_tasks SET status=%s,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=%s AND account_id=%s AND conversation_id=%s AND status='active'",
            (status, UUID(task_id), account, conversation),
        )

    def summaries(self, account: UUID, conversation: UUID) -> str:
        """仅序列化任务摘要，不在普通聊天恢复过去的制作工作流。"""
        return json.dumps(self.list(account, conversation), ensure_ascii=False, default=str)
