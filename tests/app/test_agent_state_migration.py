"""验证唯一 State 升级只清理旧图检查点，不丢弃业务历史。"""

from pathlib import Path
from uuid import UUID

import httpx

from general_agent.infrastructure.checkpoints import Checkpoints
from general_agent.infrastructure.database import Database
from tests.agent.test_multi_agent import response
from tests.auth.support import authenticated_client
from tests.attachments.test_attachments import configure_settings


def test_state_upgrade_discards_old_graph_and_preserves_messages(database_url: str, tmp_path: Path) -> None:
    """模拟版本切换，旧暂停执行终结，业务消息保留，迁移重复执行不清新状态。"""
    configure_settings(database_url, tmp_path)

    def provider(_request: httpx.Request) -> httpx.Response:
        """建立真实主执行及检查点，不连接外部模型。"""
        return response('应保留的原回答')

    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        conversation = client.post('/api/conversations', json={}).json()['id']
        assert client.post(f'/api/conversations/{conversation}/messages',
                           json={'content': '应保留的用户消息'}).status_code == 200
        messages = client.get(f'/api/conversations/{conversation}').json()['messages']
        answer_id = UUID(messages[-1]['id'])
    database = Database(database_url)
    checkpoints = Checkpoints(database)
    assert checkpoints.values(conversation)['finished'] is True
    with database.connect() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version='069_single_agent_state.sql'")
        connection.execute("UPDATE messages SET status='interrupted' WHERE id=%s", (answer_id,))
        connection.execute('UPDATE agents SET closed=FALSE WHERE answer_attempt_id=%s', (answer_id,))
        connection.execute("UPDATE agent_runs SET status='running',finished_at=NULL WHERE agent_id IN "
                           '(SELECT id FROM agents WHERE answer_attempt_id=%s)', (answer_id,))
    database.migrate()
    checkpoints.setup()
    assert checkpoints.values(conversation) == {}
    with database.connect() as connection:
        rows = connection.execute('SELECT role,content,status FROM messages WHERE conversation_id=%s ORDER BY position',
                                  (UUID(conversation),)).fetchall()
        assert [(row['role'], row['content']) for row in rows] == [
            ('user', '应保留的用户消息'), ('assistant', '应保留的原回答')]
        assert rows[-1]['status'] == 'failed'
        assert not connection.execute('SELECT 1 FROM checkpoint_threads').fetchone()
        assert not connection.execute("SELECT 1 FROM agent_runs WHERE status IN ('queued','running')").fetchone()
        connection.execute('CREATE TABLE langgraph.migration_idempotency_marker (value INTEGER)')
    database.migrate()
    with database.connect() as connection:
        marker = connection.execute("SELECT to_regclass('langgraph.migration_idempotency_marker') AS name").fetchone()
        assert marker and marker['name']
