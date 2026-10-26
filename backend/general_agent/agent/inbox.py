"""通过统一结果信箱交付子执行终态，等待不占子任务工作线程。"""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from general_agent.agent.repository import AgentRepository, RunCheckpoint

DeliveryValidator = Callable[[dict[str, Any]], None]


class AgentInbox:
    """把交付、幂等、来源登记和通知等待集中在当前回答身份下。"""

    def __init__(self, repository: AgentRepository) -> None:
        """复用仓库的提交后通知锁，数据库始终是结果状态的依据。"""
        self.repository = repository

    def collect(self, checkpoint: RunCheckpoint, key: str, channel: str = 'automatic',
                validate: DeliveryValidator | None = None) -> dict[str, Any]:
        """原子保存交付批次及来源，重放同一身份时重新鉴权并返回原结果。"""
        checkpoint.guard()
        parent = self.repository.info(checkpoint.run_id)
        if parent['parent_agent_id'] is not None:
            raise ValueError('子智能体不能读取主智能体信箱。')
        with self.repository.database.connect() as connection:
            self.repository.lock_active(connection, checkpoint.run_id)
            cached = connection.execute('SELECT payload FROM agent_inbox_deliveries WHERE recipient_run_id=%s AND delivery_key=%s', (checkpoint.run_id, key)).fetchone()
            if cached:
                cached_result = dict(cached['payload'])
                if validate is not None:
                    validate(cached_result)
                return cached_result
            rows = connection.execute('SELECT e.id AS event_id,a.id AS agent_id,r.id AS run_id,r.status,r.result,r.error FROM agent_inbox_events e JOIN agent_runs r ON r.id=e.sender_run_id JOIN agents a ON a.id=r.agent_id WHERE e.recipient_run_id=%s AND e.delivery_id IS NULL ORDER BY e.id', (checkpoint.run_id,)).fetchall()
            count = connection.execute("SELECT count(*) AS n FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.parent_agent_id=%s AND r.status IN ('queued','running')", (parent['agent_id'],)).fetchone()
            active = int(count['n']) if count else 0
            agents = []
            for row in rows:
                agents.append({**row, 'agent_id': str(row['agent_id']), 'run_id': str(row['run_id']),
                    'sources': [item['payload'] for item in connection.execute('SELECT payload FROM agent_sources WHERE run_id=%s ORDER BY source_key', (row['run_id'],)).fetchall()]})
            result: dict[str, Any] = {'status': 'results' if rows else 'pending' if active else 'idle',
                                      'agents': agents, 'active_count': active}
            if rows or channel == 'wait' and not active:
                result['delivery_id'] = str(uuid4())
                if validate is not None:
                    validate(result)
                connection.execute('INSERT INTO agent_inbox_deliveries(id,recipient_run_id,delivery_key,channel,payload) VALUES(%s,%s,%s,%s,%s)', (result['delivery_id'], checkpoint.run_id, key, channel, Jsonb(result)))
                if rows:
                    connection.execute('UPDATE agent_inbox_events SET delivery_id=%s WHERE id=ANY(%s)', (result['delivery_id'], [row['event_id'] for row in rows]))
                    connection.execute('UPDATE agent_sources SET delivered=TRUE WHERE run_id=ANY(%s)', ([row['run_id'] for row in rows],))
        return result

    def wait(self, checkpoint: RunCheckpoint, key: str,
             validate: DeliveryValidator | None = None) -> dict[str, Any]:
        """等待当前回答任一新终态，版本检查防丢唤醒，周期检查用于停止和超时。"""
        while True:
            with self.repository.condition:
                version = self.repository.notification_version
            result = self.collect(checkpoint, key, 'wait', validate)
            if result['status'] != 'pending':
                return result
            with self.repository.condition:
                if version == self.repository.notification_version:
                    self.repository.condition.wait(timeout=1)

    def settle(self, checkpoint: RunCheckpoint, answer: str, messages: list[dict[str, Any]]) -> bool:
        """在根锁内确认无未读和活动任务再关闭分派，完成事件无法穿过收尾检查。"""
        checkpoint.guard()
        parent = self.repository.info(checkpoint.run_id)
        if parent['parent_agent_id'] is not None:
            raise ValueError('子智能体不能结束主回答。')
        with self.repository.database.connect() as connection:
            self.repository.lock_active(connection, checkpoint.run_id)
            unread = connection.execute('SELECT id FROM agent_inbox_events WHERE recipient_run_id=%s AND delivery_id IS NULL LIMIT 1', (checkpoint.run_id,)).fetchone()
            active = connection.execute("SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.parent_agent_id=%s AND r.status IN ('queued','running') LIMIT 1", (parent['agent_id'],)).fetchone()
            if unread or active:
                return False
            clean = [{key: value for key, value in item.items() if key != 'reasoning_content'} for item in messages]
            connection.execute("UPDATE agent_runs SET status='completed',result=%s,messages=%s,finished_at=CURRENT_TIMESTAMP WHERE id=%s", (answer, Jsonb(clean), checkpoint.run_id))
            connection.execute('UPDATE agents SET closed=TRUE WHERE answer_attempt_id=%s', (parent['answer_attempt_id'],))
        self.repository.notify()
        return True
