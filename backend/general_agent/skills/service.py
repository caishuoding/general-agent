"""以账号与会话为授权单位保存 Skill 制作任务和可续接的工作文件。"""

import hashlib
import json
import mimetypes
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.agent.repository import AgentRepository
from general_agent.infrastructure.database import Database
from general_agent.skills.catalog import SkillCatalog


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
            return connection.execute('SELECT id,skill,version,status,stage,route,updated_at FROM skill_tasks WHERE account_id=%s AND conversation_id=%s ORDER BY updated_at DESC LIMIT 30', (account, conversation)).fetchall()

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
                connection.execute("UPDATE skill_tasks SET status='revoked',stage='资料范围已变化' WHERE id=%s", (task,))
            raise ValueError('制作任务资料范围已变化，旧工作文件已清理，请重新创建任务。')
        return row

    def activate(self, account: UUID, conversation: UUID, skill: str, task: UUID | None) -> dict[str, Any]:
        """新建或显式续接固定版本任务；不根据用户关键词自动选择 Skill。"""
        self.conversation(account, conversation)
        entry = self.catalog.read(skill, 'SKILL.md')
        if task is not None:
            row = self.get(task, account, conversation)
            if row['skill'] != skill or row['entry_digest'] != entry['digest'] or row['version'] != entry['version']:
                raise ValueError('任务 Skill 版本与安装版本不一致，不能静默升级旧任务。')
            return row
        identifier = uuid4()
        snapshot = self.repository.snapshot(conversation)
        files = {'SKILL.md': entry['file_digest']}
        with self.database.connect() as connection:
            created = connection.execute('INSERT INTO skill_tasks(id,account_id,conversation_id,skill,version,entry_digest,instruction_files,source_snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
                (identifier, account, conversation, skill, entry['version'], entry['digest'], Jsonb(files), Jsonb(snapshot))).fetchone()
        assert created is not None
        (self.directory / str(identifier) / 'workspace').mkdir(parents=True, mode=0o700)
        return created

    def prepare_library(self, task: UUID, skill: str) -> Path:
        """按任务复制模板库，允许上游注册模板且不改写安装目录或其他用户资源。"""
        destination = self.directory / str(task) / 'workspace' / '.skill-library'
        if not destination.exists():
            shutil.copytree(self.catalog.directory / skill / 'templates', destination)
        return destination

    def load_file(self, task: UUID, account: UUID, conversation: UUID, path: str, instruction: bool) -> dict[str, Any]:
        """按需完整读取引用文件，仅明确选择的 Markdown 文档加入可信指令集合。"""
        row = self.get(task, account, conversation)
        if Path(path).parts and Path(path).parts[0] == 'templates':
            library = self.prepare_library(task, row['skill'])
            source = self.path(task, account, conversation, '.skill-library/' + str(Path(path).relative_to('templates')))
            if instruction:
                raise ValueError('任务模板库是可修改参考资料，请使用 instruction=false 读取。')
            if not source.is_relative_to(library) or not source.is_file() or source.stat().st_size > 2_000_000:
                raise ValueError('模板资料路径无效或文本过大。')
            return {'path': path, 'absolute_path': f'/opt/skills/{row["skill"]}/{path}', 'content': source.read_text(encoding='utf-8')}
        item = self.catalog.read(row['skill'], path)
        if instruction:
            if Path(path).suffix.lower() != '.md':
                raise ValueError('只有已安装的 Markdown 文档可以作为 Skill 指令加载。')
            files = {**row['instruction_files'], path: item['file_digest']}
            with self.database.connect() as connection:
                connection.execute('UPDATE skill_tasks SET instruction_files=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s', (Jsonb(files), task))
        return item

    def instructions(self, task: UUID, account: UUID, conversation: UUID) -> str:
        """每次模型请求重读完整的活动文件，验证摘要并拒绝执行过程中静默换版。"""
        row = self.get(task, account, conversation)
        parts = [f'已激活 Skill {row["skill"]}，版本 {row["version"]}。SKILL_DIR=/opt/skills/{row["skill"]}；工作目录=/workspace；任务 ID={task}。']
        for path, digest in row['instruction_files'].items():
            item = self.catalog.read(row['skill'], path)
            if item['file_digest'] != digest:
                raise ValueError('已激活 Skill 文件发生变化，请恢复固定版本后继续。')
            parts.append(f'可信 Skill 指令：{item["absolute_path"]}\n{item["content"]}')
        return '\n\n'.join(parts)

    def path(self, task: UUID, account: UUID, conversation: UUID, relative: str) -> Path:
        """解析任务工作路径并拒绝上级路径、符号链接和内部记录路径。"""
        self.get(task, account, conversation)
        root = self.directory / str(task) / 'workspace'
        requested = Path(relative)
        path = root / requested
        if (requested.is_absolute() or '..' in requested.parts or any(part.startswith('.runtime') for part in requested.parts)
                or not path.resolve().is_relative_to(root.resolve())):
            raise ValueError('工作文件路径无效或越界。')
        return path

    def read(self, task: UUID, account: UUID, conversation: UUID, relative: str, offset: int = 0, limit: int = 20000) -> dict[str, Any]:
        """按字符范围读取任务文本或有界列出目录，明确续读位置且不伪称完整。"""
        path = self.path(task, account, conversation, relative)
        if path.is_dir():
            entries = sorted(item.name + ('/' if item.is_dir() else '') for item in path.iterdir())
            return {'entries': entries[:500], 'has_more': len(entries) > 500}
        if path.stat().st_size > 5_000_000:
            raise ValueError('文本文件过大，请通过执行工具提取需要的范围。')
        text = path.read_text(encoding='utf-8')
        end = offset + limit
        return {'content': text[offset:end], 'total_characters': len(text), 'next_offset': end if end < len(text) else None}

    def write(self, task: UUID, account: UUID, conversation: UUID, relative: str, content: str) -> dict[str, Any]:
        """在受限工作目录保存完整 UTF-8 文本，不解释内容中的指令。"""
        path = self.path(task, account, conversation, relative)
        if len(content.encode()) > 2_000_000:
            raise ValueError('单次写入超过两百万字节。')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return {'path': '/workspace/' + relative, 'bytes': path.stat().st_size}

    def update(self, task: UUID, account: UUID, conversation: UUID, status: str, stage: str, route: str) -> dict[str, Any]:
        """保存当前阶段及等待状态，供下一条用户消息显式续接。"""
        self.get(task, account, conversation)
        if status not in {'active', 'waiting', 'completed', 'interrupted'}:
            raise ValueError('制作任务状态无效。')
        with self.database.connect() as connection:
            connection.execute('UPDATE skill_tasks SET status=%s,stage=%s,route=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s', (status, stage, route, task))
        return {'task_id': str(task), 'status': status, 'stage': stage, 'route': route}

    def publish(self, task: UUID, account: UUID, conversation: UUID, answer: UUID, relative: str) -> dict[str, Any]:
        """将真实产物复制为不可变下载副本，更新工作文件不会改写已交付版本。"""
        source = self.path(task, account, conversation, relative)
        if not source.is_file() or source.stat().st_size > 200_000_000:
            raise ValueError('产物不存在或超过 200 MB。')
        identifier = uuid4()
        storage = self.directory / str(task) / 'artifacts'
        storage.mkdir(exist_ok=True)
        destination = storage / str(identifier)
        shutil.copyfile(source, destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        mime = mimetypes.guess_type(source.name)[0] or 'application/octet-stream'
        with self.database.connect() as connection:
            connection.execute('INSERT INTO skill_artifacts(id,task_id,answer_id,filename,digest,media_type) VALUES(%s,%s,%s,%s,%s,%s)', (identifier, task, answer, source.name, digest, mime))
        return {'artifact_id': str(identifier), 'filename': source.name,
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

    def summaries(self, account: UUID, conversation: UUID) -> str:
        """仅序列化任务摘要，不在普通聊天恢复过去的制作工作流。"""
        return json.dumps(self.list(account, conversation), ensure_ascii=False, default=str)
