"""在四个公开编排工具接口验证幂等、游标、追加限制和取消。"""

from collections.abc import Iterator
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest

from general_agent.agent.orchestration import AgentOrchestrator
from general_agent.agent.repository import AgentRepository, RunCheckpoint
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationService


@pytest.fixture
def orchestration(database_url: str) -> Iterator[tuple[AgentOrchestrator, RunCheckpoint, Event]]:
    """建立隔离的有效回答来源，任务执行器由测试显式完成，检查仅通过工具返回值。"""
    database = Database(database_url)
    database.migrate()
    account, conversation, user, answer = uuid4(), uuid4(), uuid4(), uuid4()
    with database.connect() as connection:
        connection.execute("INSERT INTO users(id,username,password_hash,role) VALUES(%s,'agent_test','unused','user')", (account,))
        connection.execute("INSERT INTO conversations(id,user_id,title) VALUES(%s,%s,'编排测试')", (conversation, account))
    repository = AgentRepository(database)
    service = AgentOrchestrator(repository)
    release = Event()
    with OperationService(database).operation('chat', 'test', conversation_id=conversation, account_id=account) as (operation, _):
        with database.connect() as connection:
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,status) VALUES(%s,%s,'user','测试','complete')", (user, conversation))
            connection.execute("INSERT INTO messages(id,conversation_id,role,content,status,reply_to,operation_id) VALUES(%s,%s,'assistant','','generating',%s,%s)", (answer, conversation, user, operation.operation_id))
        root = repository.start(answer, account, conversation, operation)
        checkpoint = RunCheckpoint(repository, root['id'], user)
        try:
            yield service, checkpoint, release
        finally:
            release.set()
            service.close()


def test_creation_is_idempotent_and_cancel_is_terminal(orchestration: tuple[AgentOrchestrator, RunCheckpoint, Event]) -> None:
    """重复创建返回同一身份，取消后迟到完成不能改变终态，等待不重复交付。"""
    service, checkpoint, release = orchestration

    def execute(run_id: UUID) -> None:
        """模拟可阻塞外部执行，并尝试在取消后返回结果。"""
        release.wait(3)
        service.repository.transition(run_id, 'completed', '迟到结果')

    checkpoint.current_call = '0:create'
    request = {'title': '任务', 'task': '检查事项', 'expected_output': '结论'}
    first = service.execute(checkpoint, 'spawn_agent', request, execute)
    assert service.execute(checkpoint, 'spawn_agent', request, execute) == first
    checkpoint.current_call = '1:wait'
    pending = service.execute(checkpoint, 'wait_agents', {'agent_ids': [first['agent_id']], 'timeout_seconds': 0}, execute)
    assert pending['agents'][0]['status'] in ('queued', 'running')
    assert 'result' not in pending['agents'][0]
    checkpoint.current_call = '2:cancel'
    assert service.execute(checkpoint, 'cancel_agent', {'agent_id': first['agent_id']}, execute)['status'] == 'cancelled'
    release.set()
    checkpoint.current_call = '3:wait'
    done = service.execute(checkpoint, 'wait_agents', {'agent_ids': [first['agent_id']]}, execute)
    assert done['agents'][0]['status'] == 'cancelled'
    assert done['agents'][0]['result'] is None
    checkpoint.current_call = '4:wait'
    again = service.execute(checkpoint, 'wait_agents', {'agent_ids': [first['agent_id']], 'after_cursor': done['cursor']}, execute)
    assert 'result' not in again['agents'][0]


def test_unknown_identity_and_busy_followup_are_rejected(orchestration: tuple[AgentOrchestrator, RunCheckpoint, Event]) -> None:
    """工具不能读取其他回答身份，也不能给活动子任务并发追加上下文。"""
    service, checkpoint, release = orchestration

    def execute(run_id: UUID) -> None:
        """让任务保持活动，供公开工具验证冲突返回。"""
        release.wait(3)

    checkpoint.current_call = '0:create'
    child = service.execute(checkpoint, 'spawn_agent', {'title': '调查', 'task': '调查事项', 'expected_output': '结果'}, execute)
    checkpoint.current_call = '1:send'
    with pytest.raises(ValueError, match='agent_busy'):
        service.execute(checkpoint, 'send_agent_message', {'agent_id': child['agent_id'], 'message': '追加要求'}, execute)
    checkpoint.current_call = '2:wait'
    with pytest.raises(ValueError, match='不属于当前回答'):
        service.execute(checkpoint, 'wait_agents', {'agent_ids': [str(uuid4())], 'timeout_seconds': 0}, execute)
    with pytest.raises(ValueError):
        service.execute(checkpoint, 'spawn_agent', {'title': '任务', 'task': '事项', 'expected_output': '结果', 'account_id': str(uuid4())}, execute)
