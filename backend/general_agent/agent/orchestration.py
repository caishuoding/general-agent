"""通过四个公开工具编排异步子任务与有界后台执行。"""

import json
import logging
import time
from collections.abc import Callable
from threading import Condition, Thread
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.agent.repository import AgentRepository, RunCheckpoint, ACTIVE
from general_agent.agent.tools import SpawnInput, WaitInput, SendInput, CancelInput

LOGGER = logging.getLogger(__name__)


class AgentOrchestrator:
    """固定六个后台线程公平认领任务，主智能体等待不占工作池。"""

    def __init__(self, repository: AgentRepository) -> None:
        """保存应用级队列和配额，线程在首次分派时按需启动。"""
        self.repository = repository
        self.condition = Condition()
        self.jobs: list[tuple[UUID, UUID, UUID, Callable[[], None]]] = []
        self.running: dict[UUID, tuple[UUID, UUID]] = {}
        self.threads: list[Thread] = []
        self.stopped = False

    def submit(self, run_id: UUID, execute: Callable[[], None]) -> None:
        """数据库提交后将任务放入有界队列，重复提交不会重复执行。"""
        info = self.repository.info(run_id)
        with self.condition:
            if self.stopped or len(self.jobs) >= LIMITS.queue_size:
                self.repository.transition(run_id, 'failed', error='后台队列已满或服务正在停止。')
                raise ValueError('后台队列已满或服务正在停止。')
            if run_id in self.running or any(job[0] == run_id for job in self.jobs):
                return
            if info['status'] != 'queued':
                return
            self.jobs.append((run_id, info['answer_attempt_id'], info['account_id'], execute))
            if not self.threads:
                for index in range(LIMITS.workers):
                    thread = Thread(target=self.work, name=f'agent-worker-{index}', daemon=True)
                    self.threads.append(thread)
                    thread.start()
                monitor = Thread(target=self.monitor, name='agent-monitor', daemon=True)
                self.threads.append(monitor)
                monitor.start()
            self.condition.notify_all()

    def work(self) -> None:
        """跳过暂时无账号或回答配额的任务，避免队首阻塞其他账号。"""
        while True:
            job = None
            with self.condition:
                while job is None and not self.stopped:
                    for index, candidate in enumerate(self.jobs):
                        _, answer, account, _ = candidate
                        if sum(value[0] == answer for value in self.running.values()) < LIMITS.per_answer and sum(value[1] == account for value in self.running.values()) < LIMITS.per_account:
                            job = self.jobs.pop(index)
                            self.running[job[0]] = (answer, account)
                            break
                    if job is None:
                        self.condition.wait(timeout=0.2)
                if self.stopped:
                    return
            assert job is not None
            try:
                self.repository.guard(job[0])
                if self.repository.transition(job[0], 'running'):
                    job[3]()
            except Exception:
                LOGGER.exception('子智能体执行失败 run_id=%s', job[0])
                self.repository.transition(job[0], 'failed', error='子任务执行失败，可新建任务重试。')
            finally:
                with self.condition:
                    self.running.pop(job[0], None)
                    self.condition.notify_all()

    def monitor(self) -> None:
        """及时标记截止任务和失效根回答，唤醒等待且不依赖网络请求返回。"""
        while True:
            with self.condition:
                if self.stopped:
                    return
                self.condition.wait(timeout=0.5)
            try:
                self.repository.expire()
                with self.repository.database.connect() as connection:
                    connection.execute("UPDATE messages SET status='failed',error='回答执行超时。' WHERE status='generating' AND id IN (SELECT answer_attempt_id FROM agents WHERE parent_agent_id IS NULL AND deadline<CURRENT_TIMESTAMP)")
                    roots = connection.execute("SELECT a.answer_attempt_id FROM agents a JOIN messages m ON m.id=a.answer_attempt_id WHERE a.parent_agent_id IS NULL AND NOT a.closed AND (m.status<>'generating' OR a.deadline<CURRENT_TIMESTAMP)").fetchall()
                for root in roots:
                    self.repository.close(root['answer_attempt_id'])
            except Exception:
                LOGGER.exception('智能体超时检查暂未完成')

    def execute(self, checkpoint: RunCheckpoint, name: str, arguments: dict[str, Any], runner: Callable[[UUID], None]) -> dict[str, Any]:
        """执行绑定主身份的编排工具；重复调用保留身份并重新检查资料权限。"""
        checkpoint.guard()
        info = self.repository.info(checkpoint.run_id)
        if info['parent_agent_id'] is not None:
            raise ValueError('子智能体不能调用编排工具。')
        key = checkpoint.current_call
        if not key:
            raise ValueError('缺少后端工具调用身份。')
        if name == 'spawn_agent':
            spawn = SpawnInput.model_validate(arguments)
            task = f'任务：{spawn.task}\n背景：{spawn.context}\n预期交付：{spawn.expected_output}'
            result = self.repository.create(checkpoint.run_id, key, task, spawn.title, instructions=spawn.instructions)
        elif name == 'send_agent_message':
            send = SendInput.model_validate(arguments)
            result = self.repository.create(checkpoint.run_id, key, send.message, agent_id=UUID(send.agent_id),
                                            instructions=send.instructions)
        elif name == 'cancel_agent':
            cancel = CancelInput.model_validate(arguments)
            result = self.repository.cancel(checkpoint.run_id, UUID(cancel.agent_id))
            with self.condition:
                self.condition.notify_all()
            return result
        elif name == 'wait_agents':
            return self.wait(checkpoint, WaitInput.model_validate(arguments))
        else:
            raise ValueError('未知编排工具。')
        run_id = UUID(result['run_id'])

        def run() -> None:
            """为后台任务显式绑定执行身份，不使用线程局部状态。"""
            runner(run_id)

        self.submit(run_id, run)
        return result

    def wait(self, checkpoint: RunCheckpoint, request: WaitInput) -> dict[str, Any]:
        """等待任一新终态；结果仅交付一次，幂等重放始终重新鉴权。"""
        targets = [UUID(value) for value in request.agent_ids]
        deadline = time.monotonic() + request.timeout_seconds
        with self.repository.database.connect() as connection:
            previous = connection.execute('SELECT result FROM agent_tool_requests WHERE parent_run_id=%s AND request_key=%s', (checkpoint.run_id, checkpoint.current_call)).fetchone()
        if previous:
            checkpoint.guard()
            return dict(previous['result'])
        seen: set[str] = set()
        if request.after_cursor:
            with self.repository.database.connect() as connection:
                cursor = connection.execute("SELECT result FROM agent_tool_requests WHERE parent_run_id=%s AND result->>'cursor'=%s", (checkpoint.run_id, request.after_cursor)).fetchone()
            if not cursor:
                raise ValueError('等待游标不属于当前回答。')
            seen.update(cursor['result'].get('seen_run_ids', []))
        seen.update(checkpoint.delivered)
        while True:
            rows = self.repository.targets(checkpoint.run_id, targets)
            fresh = [row for row in rows if row['status'] not in ACTIVE and str(row['run_id']) not in seen]
            if fresh or all(row['status'] not in ACTIVE for row in rows) or time.monotonic() >= deadline:
                break
            with self.condition:
                self.condition.wait(timeout=min(0.2, max(0, deadline - time.monotonic())))
        delivered = {str(row['run_id']) for row in fresh}
        checkpoint.delivered.update(delivered)
        seen.update(delivered)
        agents = []
        for row in rows:
            item = {'agent_id': str(row['agent_id']), 'run_id': str(row['run_id']), 'status': row['status']}
            if str(row['run_id']) in delivered:
                item.update(result=row['result'], error=row['error'], sources=row['sources'])
            agents.append(item)
        result = {'cursor': str(uuid4()), 'agents': agents, 'seen_run_ids': sorted(seen)}
        checkpoint.guard()
        with self.repository.database.connect() as connection:
            self.repository.lock_active(connection, checkpoint.run_id)
            if delivered:
                connection.execute('UPDATE agent_sources SET delivered=TRUE WHERE run_id=ANY(%s)', ([UUID(value) for value in delivered],))
            connection.execute('INSERT INTO agent_tool_requests VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (checkpoint.run_id, checkpoint.current_call, Jsonb(result)))
        return result

    def recover(self) -> None:
        """服务重启终结旧执行，不自动重发模型请求。"""
        with self.repository.database.connect() as connection:
            answers = connection.execute("SELECT DISTINCT a.answer_attempt_id FROM agents a JOIN agent_runs r ON r.agent_id=a.id WHERE r.status IN ('queued','running')").fetchall()
        for answer in answers:
            with self.repository.database.connect() as connection:
                connection.execute("UPDATE agent_runs SET status='failed',error='应用重启中断了任务。',finished_at=CURRENT_TIMESTAMP WHERE status IN ('queued','running') AND agent_id IN (SELECT id FROM agents WHERE answer_attempt_id=%s)", (answer['answer_attempt_id'],))
                connection.execute('UPDATE cloud_operations SET cancelled=TRUE WHERE id IN (SELECT r.operation_id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=%s)', (answer['answer_attempt_id'],))
            self.repository.close(answer['answer_attempt_id'])

    def close(self) -> None:
        """取消后台任务并有界等待工作线程，不无限等待阻塞网络。"""
        with self.condition:
            self.stopped = True
            self.jobs.clear()
            self.condition.notify_all()
        self.recover()
        deadline = time.monotonic() + 2
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
