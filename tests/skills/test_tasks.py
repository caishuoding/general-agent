"""通过制作任务接口验证授权、完整指令和跨消息续接。"""

from pathlib import Path
from uuid import uuid4

import pytest

from general_agent.infrastructure.database import Database
from general_agent.skills.catalog import SkillCatalog
from general_agent.skills.service import SkillService
from tests.auth.support import ensure_normal_user


def test_task_load_resume_and_file_authorization(database_url: str, tmp_path: Path) -> None:
    """制作任务保存固定指令和文件，跨执行续接且拒绝其他账号读取。"""
    database = Database(database_url)
    database.migrate()
    ensure_normal_user(database_url)
    with database.connect() as connection:
        account = connection.execute("SELECT id FROM users WHERE username='test_user'").fetchone()
    assert account is not None
    conversation = database.create_conversation('制作汇报', account['id'])['id']
    catalog = SkillCatalog(Path('skills'))
    service = SkillService(database, tmp_path, catalog)
    task = service.activate(account['id'], conversation, 'ppt-master', None)
    assert '# PPT Master Skill' in service.instructions(task['id'], account['id'], conversation)
    service.write(task['id'], account['id'], conversation, 'draft.md', '制作方案')
    service.update(task['id'], account['id'], conversation, 'waiting', '等待配色确认', 'generate')
    resumed = service.activate(account['id'], conversation, 'ppt-master', task['id'])
    assert resumed['stage'] == '等待配色确认'
    assert service.read(task['id'], account['id'], conversation, 'draft.md')['content'] == '制作方案'
    with pytest.raises(LookupError):
        service.read(task['id'], uuid4(), conversation, 'draft.md')
    with pytest.raises(ValueError, match='路径'):
        service.write(task['id'], account['id'], conversation, '../secret', 'bad')
