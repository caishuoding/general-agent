"""在编排工具公开接口验证零参数等待、幂等交付、追加、取消与超时。"""

from collections.abc import Iterator
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest

from general_agent.agent.orchestration import AgentOrchestrator
from general_agent.agent.repository import AgentRepository, RunCheckpoint
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import CallBudgetExceeded, OperationContext, OperationService, WebKind
from general_agent.infrastructure.provider import TokenUsage
from general_agent.infrastructure.settings import StoredSettings


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
    checkpoint.current_call = '2:cancel'
    assert service.execute(checkpoint, 'cancel_agent', {'agent_id': first['agent_id']}, execute)['status'] == 'cancelled'
    release.set()
    checkpoint.current_call = '3:wait'
    done = service.execute(checkpoint, 'wait_agents', {}, execute)
    assert done['agents'][0]['status'] == 'cancelled'
    assert done['agents'][0]['result'] is None
    checkpoint.current_call = '4:wait'
    again = service.execute(checkpoint, 'wait_agents', {}, execute)
    assert again['status'] == 'idle'
    assert again['agents'] == []


def test_each_agent_has_separate_search_and_fetch_budgets(orchestration: tuple[AgentOrchestrator, RunCheckpoint, Event]) -> None:
    """输入隔离回答与子执行，核验主从各有六次搜索和六次抓取且补充执行沿用额度；无返回值。"""
    service, checkpoint, _ = orchestration
    repository = service.repository
    main = repository.info(checkpoint.run_id)
    children = []
    for index in range(2):
        # 直接创建并启动不同子身份，复用真实回答与调用账本，避免线程调度影响额度断言。
        child = repository.create(checkpoint.run_id, f'budget:{index}', '核查资料', f'研究员{index}')
        assert repository.transition(UUID(child['run_id']), 'running')
        children.append(repository.info(UUID(child['run_id'])))
    operations = OperationService(repository.database)
    settings = StoredSettings(base_url='https://search.example', model='deepseek-search', api_key='test-key')

    def context(run: dict[str, Any], purpose: str) -> OperationContext:
        """按执行轮次构造真实操作身份，输入为运行记录和用途，输出为调用账本上下文。"""
        return OperationContext(run['operation_id'], purpose, run['conversation_id'], run['account_id'])

    def spend(request: OperationContext, kind: WebKind, count: int) -> None:
        """输入操作、联网类别及次数，逐次模拟真实外发并完成计量；无返回值。"""
        for _ in range(count):
            call_id = operations.reserve(request, settings, 'credits')
            operations.begin(request, call_id, settings, web_kind=kind)
            operations.record(call_id, TokenUsage(), 'complete')

    main_context = context(main, 'chat')
    first_context = context(children[0], 'agent_worker')
    second_context = context(children[1], 'agent_worker')
    spend(main_context, 'search', 6)
    spend(first_context, 'search', 6)
    # 搜索额度用尽不应阻挡同一智能体抓取，也不应占用其他子智能体的搜索额度。
    spend(main_context, 'fetch', 6)
    spend(first_context, 'fetch', 6)
    spend(second_context, 'search', 6)
    spend(second_context, 'fetch', 6)
    for request in (main_context, first_context, second_context):
        for kind, label in (('search', '搜索'), ('fetch', '抓取')):
            call_id = operations.reserve(request, settings, 'credits')
            with pytest.raises(CallBudgetExceeded, match=f'智能体.*6次{label}请求'):
                operations.begin(request, call_id, settings, web_kind=kind)
            operations.cancel_reservation(call_id)
    # 同一子智能体再次接任务不能借新执行轮次获得额外六次。
    assert repository.transition(children[0]['id'], 'completed', '首次核查完成')
    followup = repository.create(checkpoint.run_id, 'budget:followup', '补充核查', agent_id=children[0]['agent_id'])
    assert repository.transition(UUID(followup['run_id']), 'running')
    followup_context = context(repository.info(UUID(followup['run_id'])), 'agent_worker')
    for kind, label in (('search', '搜索'), ('fetch', '抓取')):
        call_id = operations.reserve(followup_context, settings, 'credits')
        with pytest.raises(CallBudgetExceeded, match=f'智能体.*6次{label}请求'):
            operations.begin(followup_context, call_id, settings, web_kind=kind)
        operations.cancel_reservation(call_id)
    with repository.database.connect() as connection:
        counts = connection.execute('SELECT search_count,fetch_count FROM agents WHERE answer_attempt_id=%s ORDER BY id', (main['answer_attempt_id'],)).fetchall()
    assert [(row['search_count'], row['fetch_count']) for row in counts] == [(6, 6)] * 3


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
        service.execute(checkpoint, 'cancel_agent', {'agent_id': str(uuid4())}, execute)
    with pytest.raises(ValueError):
        service.execute(checkpoint, 'spawn_agent', {'title': '任务', 'task': '事项', 'expected_output': '结果', 'account_id': str(uuid4())}, execute)


def test_wait_without_targets_returns_idle(orchestration: tuple[AgentOrchestrator, RunCheckpoint, Event]) -> None:
    """零参数等待在没有子任务时立即返回空闲，不要求模型管理目标或游标。"""
    service, checkpoint, _ = orchestration

    def execute(run_id: UUID) -> None:
        """本用例不应分派任何后台任务。"""
        raise AssertionError('不应创建子任务')

    checkpoint.current_call = '0:wait'
    result = service.execute(checkpoint, 'wait_agents', {}, execute)
    assert result['status'] == 'idle'
    assert result['agents'] == []
    assert result['active_count'] == 0


def test_wait_delivers_first_finished_run_and_followup_once(orchestration: tuple[AgentOrchestrator, RunCheckpoint, Event]) -> None:
    """慢任务不挡住先完成结果，工具重放幂等，补充执行有独立的新交付。"""
    service, checkpoint, release = orchestration

    def execute(run_id: UUID) -> None:
        """让第一个任务等待，其他任务立即交付供公开等待接口验收。"""
        task = service.repository.info(run_id)['task']
        if '慢任务' in task:
            assert release.wait(8)
        service.repository.transition(run_id, 'completed', '公开结果：' + task)

    checkpoint.current_call = '0:slow'
    slow = service.execute(checkpoint, 'spawn_agent', {'title': '慢任务', 'task': '慢任务', 'expected_output': '结论'}, execute)
    checkpoint.current_call = '1:fast'
    fast = service.execute(checkpoint, 'spawn_agent', {'title': '快任务', 'task': '快任务', 'expected_output': '结论'}, execute)
    checkpoint.current_call = '2:wait'
    first = service.execute(checkpoint, 'wait_agents', {}, execute)
    assert [item['agent_id'] for item in first['agents']] == [fast['agent_id']]
    assert first['active_count'] == 1
    assert service.execute(checkpoint, 'wait_agents', {}, execute) == first
    checkpoint.current_call = '3:followup'
    followup = service.execute(checkpoint, 'send_agent_message', {'agent_id': fast['agent_id'], 'message': '补充核验'}, execute)
    checkpoint.current_call = '4:wait'
    second = service.execute(checkpoint, 'wait_agents', {}, execute)
    assert [item['run_id'] for item in second['agents']] == [followup['run_id']]
    assert followup['run_id'] != fast['run_id']
    checkpoint.current_call = '5:cancel'
    service.execute(checkpoint, 'cancel_agent', {'agent_id': slow['agent_id']}, execute)
    checkpoint.current_call = '6:wait'
    cancelled = service.execute(checkpoint, 'wait_agents', {}, execute)
    assert cancelled['agents'][0]['status'] == 'cancelled'
    release.set()
    checkpoint.current_call = '7:wait'
    assert service.execute(checkpoint, 'wait_agents', {}, execute)['status'] == 'idle'


def test_wait_returns_child_failure_as_a_delivery(orchestration: tuple[AgentOrchestrator, RunCheckpoint, Event]) -> None:
    """子任务失败属于成功取得的终态通知，不能误判成等待工具自身失败。"""
    service, checkpoint, _ = orchestration

    def execute(run_id: UUID) -> None:
        """模拟真实后台任务执行异常，由调度器保存失败终态。"""
        raise ValueError('模拟研究失败')

    checkpoint.current_call = '0:spawn'
    child = service.execute(checkpoint, 'spawn_agent', {'title': '失败研究', 'task': '研究', 'expected_output': '结论'}, execute)
    checkpoint.current_call = '1:wait'
    result = service.execute(checkpoint, 'wait_agents', {}, execute)
    assert result['status'] == 'results'
    assert result['agents'][0]['run_id'] == child['run_id']
    assert result['agents'][0]['status'] == 'failed'
    assert result['agents'][0]['error']


def test_wait_is_woken_by_run_timeout(orchestration: tuple[AgentOrchestrator, RunCheckpoint, Event], monkeypatch: pytest.MonkeyPatch) -> None:
    """排队与运行期限到达后交付超时，等待不必等阻塞的外部执行器退出。"""
    from dataclasses import replace
    from general_agent.infrastructure.agent_limits import LIMITS
    service, checkpoint, release = orchestration
    monkeypatch.setattr('general_agent.agent.repository.LIMITS', replace(LIMITS, run_seconds=1))

    def execute(run_id: UUID) -> None:
        """保持任务阻塞，超时终态由后台监控独立设置。"""
        assert release.wait(8)

    checkpoint.current_call = '0:spawn'
    child = service.execute(checkpoint, 'spawn_agent', {'title': '超时任务', 'task': '等待外部请求', 'expected_output': '结果'}, execute)
    checkpoint.current_call = '1:wait'
    result = service.execute(checkpoint, 'wait_agents', {}, execute)
    assert result['agents'][0]['run_id'] == child['run_id']
    assert result['agents'][0]['status'] == 'timed_out'
    release.set()
