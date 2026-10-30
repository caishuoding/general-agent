"""通过用户文件接口验证模板上传、真实交付和跨账号授权。"""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from tests.auth.support import authenticated_client, ensure_normal_user, login_user
from general_agent.infrastructure.database import Database
from general_agent.skills.catalog import SkillCatalog
from general_agent.skills.service import SkillService


def test_native_template_upload_and_immutable_artifact_are_account_scoped(database_url: str, tmp_path: Path) -> None:
    """用户可上传 PPTX 原件并下载不可变交付副本，其他账号不能读会话文件。"""
    package = BytesIO()
    with ZipFile(package, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('ppt/presentation.xml', '<presentation/>')
    with authenticated_client(database_url, tmp_path) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        uploaded = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('公司模板.pptx', package.getvalue())})
        assert uploaded.status_code == 201, uploaded.text
        rows = client.get(f'/api/conversations/{conversation}/attachments').json()
        assert rows[0]['status'] == 'ready'
        database = Database(database_url)
        account = client.get('/api/auth/me').json()['id']
        from uuid import UUID, uuid4
        service = SkillService(database, tmp_path, SkillCatalog(Path('skills')))
        task = service.activate(UUID(account), UUID(conversation), 'ppt-master', None)
        service.write(task['id'], UUID(account), UUID(conversation), 'report.txt', '原交付内容')
        with database.connect() as connection:
            answer = uuid4()
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,status) VALUES(%s,%s,'assistant','已交付','complete')", (answer, UUID(conversation)))
        artifact = service.publish(task['id'], UUID(account), UUID(conversation), answer, 'report.txt')
        service.write(task['id'], UUID(account), UUID(conversation), 'report.txt', '后来的修改')
        assert client.get(artifact['url']).text == '原交付内容'
        assert client.get(f'/api/conversations/{conversation}/skill-tasks').json()[0]['artifacts'][0]['filename'] == 'report.txt'
        ensure_normal_user(database_url, 'another_user')
        login_user(client, 'another_user')
        assert client.get(artifact['url']).status_code == 404
        assert client.get(f'/api/conversations/{conversation}/skill-tasks').status_code == 404
