"""管理会话 Skill 名称、会话工作区、Run 命令目录和不可变生成文件。"""

import hashlib
import mimetypes
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from general_agent.infrastructure.database import Database
from general_agent.skills.catalog import SkillCatalog
from general_agent.skills.files import SafeFiles


class SkillService:
    """将 Skill、文件与命令绑定到现有账号、会话、回答和 Agent Run。"""

    def __init__(self, database: Database, directory: Path, catalog: SkillCatalog) -> None:
        """绑定业务存储、生成文件根目录与只读 Skill 目录。"""
        self.database, self.catalog = database, catalog
        self.directory = directory.resolve() / 'generated'

    def conversation(self, account: UUID, conversation: UUID) -> None:
        """每次访问核对账号与未删除会话，其他账号一律视为不存在。"""
        with self.database.connect() as connection:
            row = connection.execute(
                'SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting',
                (conversation, account),
            ).fetchone()
        if row is None:
            raise LookupError('会话不存在。')

    def run(self, account: UUID, conversation: UUID, run_id: UUID) -> dict[str, Any]:
        """复核 Agent Run 属于当前账号和会话，返回回答及执行身份。"""
        self.conversation(account, conversation)
        with self.database.connect() as connection:
            row = connection.execute(
                'SELECT r.id,a.account_id,a.conversation_id '
                'FROM agent_runs r JOIN agents a ON a.id=r.agent_id '
                'WHERE r.id=%s AND a.account_id=%s AND a.conversation_id=%s '
                'AND a.parent_agent_id IS NULL',
                (run_id, account, conversation),
            ).fetchone()
        if row is None:
            raise LookupError('Agent 执行不存在。')
        return row

    def remember_skills(self, connection: Any, conversation: UUID, names: list[str]) -> None:
        """在发送事务内记录会话使用过的 Skill 名称，供后续消息继续读取。"""
        for name in dict.fromkeys(names):
            connection.execute(
                'INSERT INTO conversation_skills(conversation_id,skill_name) VALUES(%s,%s) '
                'ON CONFLICT(conversation_id,skill_name) DO UPDATE SET last_used_at=CURRENT_TIMESTAMP',
                (conversation, name),
            )

    def conversation_skills(self, account: UUID, conversation: UUID) -> list[str]:
        """按首次使用顺序返回会话中所有已使用 Skill 名称。"""
        self.conversation(account, conversation)
        with self.database.connect() as connection:
            rows = connection.execute(
                'SELECT skill_name FROM conversation_skills WHERE conversation_id=%s '
                'ORDER BY first_used_at,skill_name',
                (conversation,),
            ).fetchall()
        return [str(row['skill_name']) for row in rows]

    def conversation_directory(self, account: UUID, conversation: UUID) -> Path:
        """返回已授权会话的生成根目录，不向模型暴露宿主路径。"""
        self.conversation(account, conversation)
        return self.directory / str(account) / str(conversation)

    def workspace(self, account: UUID, conversation: UUID, create: bool = False) -> Path:
        """返回或按需创建会话共享 workspace，所有可编辑项目限制在这里。"""
        root = self.conversation_directory(account, conversation) / 'workspace'
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def command_directory(self, account: UUID, conversation: UUID, run_id: UUID) -> Path:
        """校验当前 Run 后返回会话级命令日志目录。"""
        self.run(account, conversation, run_id)
        root = self.conversation_directory(account, conversation) / 'commands'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def prepare_libraries(self, account: UUID, conversation: UUID,
                          skills: list[str]) -> dict[str, Path]:
        """为当前会话复制每个已用 Skill 的模板库，避免跨会话互相修改。"""
        root = self.conversation_directory(account, conversation) / 'libraries'
        libraries: dict[str, Path] = {}
        for skill in skills:
            source = self.catalog.directory / skill / 'templates'
            if not source.is_dir():
                continue
            destination = root / skill
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copytree(source, destination)
            libraries[skill] = destination
        return libraries

    def read_work(self, account: UUID, conversation: UUID, run_id: UUID,
                  relative: str, offset: int, limit: int) -> dict[str, Any]:
        """按范围读取当前会话 workspace 文本或目录清单，并复核 Run 身份。"""
        self.run(account, conversation, run_id)
        return SafeFiles(self.workspace(account, conversation)).read(relative, offset, limit)

    def write_work(self, account: UUID, conversation: UUID, run_id: UUID,
                   relative: str, content: str) -> dict[str, Any]:
        """在会话 workspace 内保存文本，并用当前 Run 复核写入权限。"""
        self.run(account, conversation, run_id)
        data = content.encode('utf-8')
        if len(data) > 2_000_000:
            raise ValueError('单次写入超过两百万字节。')
        root = self.workspace(account, conversation, create=True)
        with SafeFiles(root).file(relative, write=True) as destination:
            destination.write(data)
        return {'path': relative, 'bytes': len(data)}

    def publish(self, account: UUID, conversation: UUID, run_id: UUID,
                relative: str, file_id: UUID | None = None) -> dict[str, Any]:
        """保存会话生成文件的不可变副本，并记录创建它的 Run。"""
        self.run(account, conversation, run_id)
        identifier = file_id or uuid4()
        with self.database.connect() as connection:
            existing = connection.execute(
                'SELECT filename FROM generated_files WHERE id=%s AND account_id=%s '
                'AND conversation_id=%s AND created_by_run_id=%s',
                (identifier, account, conversation, run_id),
            ).fetchone()
        if existing:
            return {'file_id': str(identifier), 'filename': existing['filename'],
                    'url': f'/api/conversations/{conversation}/generated-files/{identifier}/file'}
        root = self.workspace(account, conversation)
        with SafeFiles(root).file(relative) as source:
            data = source.read(200_000_001)
        if len(data) > 200_000_000:
            raise ValueError('生成文件超过 200 MB。')
        storage = self.conversation_directory(account, conversation) / 'artifacts'
        storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = storage / str(identifier)
        temporary = storage / (str(identifier) + '.tmp')
        temporary.write_bytes(data)
        temporary.replace(destination)
        digest = hashlib.sha256(data).hexdigest()
        filename = Path(relative).name
        mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        with self.database.connect() as connection:
            connection.execute(
                'INSERT INTO generated_files(id,account_id,conversation_id,created_by_run_id,'
                'path,filename,digest,media_type) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                (identifier, account, conversation, run_id, str(destination), filename, digest, mime),
            )
        return {'file_id': str(identifier), 'filename': filename,
                'url': f'/api/conversations/{conversation}/generated-files/{identifier}/file'}

    def list_files(self, account: UUID, conversation: UUID) -> list[dict[str, Any]]:
        """返回当前会话最近生成的文件，不暴露其他会话或用户记录。"""
        self.conversation(account, conversation)
        with self.database.connect() as connection:
            return connection.execute(
                'SELECT id,filename,media_type,created_at FROM generated_files '
                'WHERE account_id=%s AND conversation_id=%s ORDER BY created_at DESC,id DESC LIMIT 100',
                (account, conversation),
            ).fetchall()

    def artifact(self, file_id: UUID, account: UUID, conversation: UUID) -> tuple[Path, dict[str, Any]]:
        """下载前重新验证账号、会话和文件摘要。"""
        with self.database.connect() as connection:
            row = connection.execute(
                'SELECT * FROM generated_files WHERE id=%s AND account_id=%s AND conversation_id=%s',
                (file_id, account, conversation),
            ).fetchone()
        if row is None:
            raise LookupError('生成文件不存在。')
        path = Path(str(row['path']))
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['digest']:
            raise LookupError('生成文件已丢失或变化。')
        return path, row

    def preview(self, token_digest: str) -> dict[str, Any] | None:
        """按能力令牌摘要读取未过期预览及有效用户状态。"""
        with self.database.connect() as connection:
            return connection.execute(
                'SELECT p.*,u.status AS user_status FROM skill_previews p '
                'JOIN users u ON u.id=p.account_id '
                'WHERE p.token_digest=%s AND p.expires_at>CURRENT_TIMESTAMP',
                (token_digest,),
            ).fetchone()
