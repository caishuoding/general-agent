"""保存回答内的智能体执行身份、独立步骤、状态和真实证据。"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from general_agent.infrastructure.agent_limits import LIMITS
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext

ACTIVE = ('queued', 'running')


class AgentRepository:
    """通过根回答行锁串行化分派，业务记录不充当图恢复检查点。"""

    def __init__(self, database: Database) -> None:
        """绑定业务数据库，每次操作使用独立事务。"""
        self.database = database

    def snapshot(self, conversation_id: UUID) -> dict[str, Any]:
        """取得可访问资料版本清单；变化时保守中止，避免旧上下文泄露撤销内容。"""
        with self.database.connect() as connection:
            attachments = connection.execute("SELECT id,status,expires_at FROM attachments WHERE conversation_id=%s AND status='ready' AND expires_at>CURRENT_TIMESTAMP ORDER BY id", (conversation_id,)).fetchall()
            knowledge = connection.execute('SELECT s.knowledge_base_id,d.id,d.attempt,d.status,b.deleting FROM conversation_knowledge_bases s JOIN knowledge_bases b ON b.id=s.knowledge_base_id LEFT JOIN knowledge_documents d ON d.knowledge_base_id=b.id WHERE s.conversation_id=%s ORDER BY s.knowledge_base_id,d.id', (conversation_id,)).fetchall()
        return dict(json.loads(json.dumps({'attachments': attachments, 'knowledge': knowledge}, default=str)))

    def start(self, answer_id: UUID, account_id: UUID, conversation_id: UUID, operation: OperationContext) -> dict[str, Any]:
        """为正在生成的回答登记主执行；同一回答重复进入返回原执行。"""
        snapshot = self.snapshot(conversation_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT id FROM messages WHERE id=%s AND conversation_id=%s AND status='generating' FOR UPDATE", (answer_id, conversation_id)).fetchone()
            if not row or not connection.execute('SELECT id FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting', (conversation_id, account_id)).fetchone():
                raise ValueError('回答已停止或会话不可用。')
            existing = connection.execute('SELECT r.* FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=%s AND a.parent_agent_id IS NULL', (answer_id,)).fetchone()
            if existing:
                return existing
            agent_id, run_id = uuid4(), uuid4()
            deadline = datetime.now(timezone.utc) + timedelta(seconds=LIMITS.answer_seconds)
            connection.execute('INSERT INTO agents(id,answer_attempt_id,account_id,conversation_id,title,deadline,source_snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s)', (agent_id, answer_id, account_id, conversation_id, '主智能体', deadline, Jsonb(snapshot)))
            return connection.execute("INSERT INTO agent_runs(id,agent_id,sequence,operation_id,request_key,task,status,started_at,deadline) VALUES(%s,%s,1,%s,'main','主回答','running',CURRENT_TIMESTAMP,%s) RETURNING *", (run_id, agent_id, operation.operation_id, deadline)).fetchone() or {}

    def info(self, run_id: UUID) -> dict[str, Any]:
        """取得执行与根回答身份，供受信任后端绑定工具参数。"""
        with self.database.connect() as connection:
            row = connection.execute('SELECT r.*,a.answer_attempt_id,a.account_id,a.conversation_id,a.parent_agent_id,a.title,a.closed FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE r.id=%s', (run_id,)).fetchone()
        if row is None:
            raise ValueError('智能体执行不存在。')
        return row

    def guard(self, run_id: UUID) -> None:
        """每次执行前检查根回答、子任务和资料版本；失效资料不再进入模型。"""
        row = self.info(run_id)
        with self.database.connect() as connection:
            root = connection.execute('SELECT a.* FROM agents a WHERE a.answer_attempt_id=%s AND a.parent_agent_id IS NULL', (row['answer_attempt_id'],)).fetchone()
            message = connection.execute("SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id JOIN cloud_operations o ON o.id=m.operation_id WHERE m.id=%s AND m.status='generating' AND NOT c.deleting AND NOT o.cancelled AND NOT o.invalidated", (row['answer_attempt_id'],)).fetchone()
        if not root or not message or root['closed'] or row['status'] not in ACTIVE or row['deadline'] <= datetime.now(timezone.utc) or root['deadline'] <= datetime.now(timezone.utc):
            raise ValueError('智能体执行已停止、超时或来源不可用。')
        if root['source_snapshot'] != self.snapshot(row['conversation_id']):
            from general_agent.infrastructure.agent_cleanup import invalidate_agent_material
            with self.database.connect() as connection:
                invalidate_agent_material(connection, [row['conversation_id']], [row['answer_attempt_id']])
            raise ValueError('source_invalidated：资料范围已变化，旧任务结果与上下文已清除，请重新提问。')

    def create(self, parent_run: UUID, key: str, task: str, title: str = '', agent_id: UUID | None = None,
               instructions: str | None = None) -> dict[str, Any]:
        """原子分派或补充任务，按调用身份幂等，并保存本轮任务级指令。"""
        self.guard(parent_run)
        parent = self.info(parent_run)
        if parent['parent_agent_id'] is not None:
            raise ValueError('子智能体不能创建任务。')
        with self.database.connect() as connection:
            root = connection.execute('SELECT * FROM agents WHERE id=%s FOR UPDATE', (parent['agent_id'],)).fetchone()
            if not root or root['closed']:
                raise ValueError('回答已关闭分派。')
            cached = connection.execute('SELECT result FROM agent_tool_requests WHERE parent_run_id=%s AND request_key=%s', (parent_run, key)).fetchone()
            if cached:
                return dict(cached['result'])
            history: list[dict[str, Any]] = []
            sequence = 1
            if agent_id is None:
                count = connection.execute('SELECT count(*) AS n FROM agents WHERE parent_agent_id=%s', (root['id'],)).fetchone()
                if count and count['n'] >= LIMITS.max_agents:
                    raise ValueError('子智能体创建达到六个上限。')
                agent_id = uuid4()
                connection.execute('INSERT INTO agents(id,answer_attempt_id,account_id,conversation_id,parent_agent_id,title,deadline) VALUES(%s,%s,%s,%s,%s,%s,%s)', (agent_id, root['answer_attempt_id'], root['account_id'], root['conversation_id'], root['id'], title, root['deadline']))
            else:
                agent = connection.execute('SELECT id FROM agents WHERE id=%s AND parent_agent_id=%s', (agent_id, root['id'])).fetchone()
                if not agent:
                    raise ValueError('子智能体不属于当前回答。')
                previous = connection.execute('SELECT * FROM agent_runs WHERE agent_id=%s ORDER BY sequence DESC LIMIT 1', (agent_id,)).fetchone()
                if not previous or previous['status'] != 'completed':
                    raise ValueError('agent_busy：只能给已完成的子智能体追加任务。')
                sequence = previous['sequence'] + 1
                if sequence > LIMITS.max_runs:
                    raise ValueError('子智能体最多执行三次任务。')
                history = previous['messages']
                if instructions is None:
                    instructions = str(previous.get('instructions') or '')
            instructions = instructions or ''
            operation_id, run_id = uuid4(), uuid4()
            connection.execute("INSERT INTO cloud_operations(id,scope_hash,purpose,conversation_id,user_id) VALUES(%s,%s,'agent_worker',%s,%s)", (operation_id, str(run_id), root['conversation_id'], root['account_id']))
            deadline = min(root['deadline'], datetime.now(timezone.utc) + timedelta(seconds=LIMITS.run_seconds))
            connection.execute("INSERT INTO agent_runs(id,agent_id,sequence,operation_id,request_key,task,status,messages,deadline,instructions) VALUES(%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s)", (run_id, agent_id, sequence, operation_id, key, task, Jsonb(history), deadline, instructions))
            if sequence > 1:
                connection.execute('INSERT INTO agent_sources(run_id,source_key,payload,delivered) SELECT %s,s.source_key,s.payload,s.delivered FROM agent_sources s JOIN agent_runs prior ON prior.id=s.run_id WHERE prior.agent_id=%s ON CONFLICT DO NOTHING', (run_id, agent_id))
            result = {'agent_id': str(agent_id), 'run_id': str(run_id), 'status': 'queued'}
            connection.execute('INSERT INTO agent_tool_requests VALUES(%s,%s,%s)', (parent_run, key, Jsonb(result)))
            return result

    def targets(self, parent_run: UUID, agent_ids: list[UUID]) -> list[dict[str, Any]]:
        """只读取本次主执行创建的子智能体最新任务及真实来源。"""
        self.guard(parent_run)
        parent = self.info(parent_run)
        if len(set(agent_ids)) != len(agent_ids):
            raise ValueError('等待目标不能重复。')
        with self.database.connect() as connection:
            rows = connection.execute('SELECT DISTINCT ON (a.id) a.id AS agent_id,a.title,r.id AS run_id,r.status,r.task,r.result,r.error,r.sequence FROM agents a JOIN agent_runs r ON r.agent_id=a.id WHERE a.parent_agent_id=%s AND a.id=ANY(%s) ORDER BY a.id,r.sequence DESC', (parent['agent_id'], agent_ids)).fetchall()
            if len(rows) != len(agent_ids):
                raise ValueError('子智能体不存在或不属于当前回答。')
            for row in rows:
                row['sources'] = [item['payload'] for item in connection.execute('SELECT payload FROM agent_sources WHERE run_id=%s', (row['run_id'],)).fetchall()]
        return rows

    def lock_active(self, connection: Any, run_id: UUID) -> None:
        """在写入事务内锁根身份并复核活动状态，与取消及资料清理串行。"""
        row = connection.execute("SELECT root.closed,r.status,m.status AS answer_status FROM agent_runs r JOIN agents a ON a.id=r.agent_id JOIN agents root ON root.answer_attempt_id=a.answer_attempt_id AND root.parent_agent_id IS NULL JOIN messages m ON m.id=a.answer_attempt_id WHERE r.id=%s FOR UPDATE OF root", (run_id,)).fetchone()
        if not row or row['closed'] or row['status'] not in ACTIVE or row['answer_status'] != 'generating':
            raise ValueError('执行已关闭，不能提交迟到结果。')

    def transition(self, run_id: UUID, status: str, result: str | None = None, messages: list[dict[str, Any]] | None = None, error: str | None = None) -> bool:
        """仅更新活动执行，迟到的成功或失败不能重写取消终态。"""
        if messages is not None:
            messages = [{key: value for key, value in item.items() if key != 'reasoning_content'} for item in messages]
        with self.database.connect() as connection:
            if status in ('completed', 'running'):
                try:
                    self.lock_active(connection, run_id)
                except ValueError:
                    return False
            row = connection.execute("UPDATE agent_runs SET status=%s,result=%s,error=%s,messages=coalesce(%s,messages),started_at=coalesce(started_at,CURRENT_TIMESTAMP),finished_at=CASE WHEN %s='running' THEN NULL ELSE CURRENT_TIMESTAMP END WHERE id=%s AND status IN ('queued','running') RETURNING id", (status, result, error, Jsonb(messages) if messages is not None else None, status, run_id)).fetchone()
        return row is not None

    def cancel(self, parent_run: UUID, agent_id: UUID) -> dict[str, Any]:
        """幂等取消属于当前回答的活动子执行，并撤回其外发许可。"""
        row = self.targets(parent_run, [agent_id])[0]
        if row['status'] in ACTIVE:
            with self.database.connect() as connection:
                self.lock_active(connection, parent_run)
                updated = connection.execute("UPDATE agent_runs SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status IN ('queued','running') RETURNING operation_id", (row['run_id'],)).fetchone()
                if updated:
                    connection.execute('UPDATE cloud_operations SET cancelled=TRUE WHERE id=%s', (updated['operation_id'],))
            row = self.targets(parent_run, [agent_id])[0]
        return {'agent_id': str(agent_id), 'run_id': str(row['run_id']), 'status': row['status']}

    def close(self, answer_id: UUID, purge: bool = False) -> None:
        """关闭根分派并取消全部子任务；来源撤销时清除执行正文及缓存。"""
        with self.database.connect() as connection:
            connection.execute('SELECT id FROM agents WHERE answer_attempt_id=%s AND parent_agent_id IS NULL FOR UPDATE', (answer_id,))
            connection.execute('UPDATE agents SET closed=TRUE WHERE answer_attempt_id=%s', (answer_id,))
            connection.execute("UPDATE cloud_operations SET cancelled=TRUE WHERE id IN (SELECT r.operation_id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=%s AND a.parent_agent_id IS NOT NULL AND r.status IN ('queued','running'))", (answer_id,))
            connection.execute("UPDATE agent_runs SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE agent_id IN (SELECT id FROM agents WHERE answer_attempt_id=%s) AND status IN ('queued','running')", (answer_id,))
            if purge:
                connection.execute("UPDATE agent_runs SET result=NULL,task='',messages='[]',error='source_invalidated' WHERE agent_id IN (SELECT id FROM agents WHERE answer_attempt_id=%s)", (answer_id,))
                for table in ('agent_sources', 'agent_messages'):
                    connection.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=%s)', (answer_id,))
                connection.execute('DELETE FROM agent_tool_requests WHERE parent_run_id IN (SELECT r.id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=%s)', (answer_id,))

    def expire(self) -> None:
        """按根锁再执行锁的顺序原子撤销超时任务及其外发许可。"""
        with self.database.connect() as connection:
            rows = connection.execute("SELECT r.id,a.answer_attempt_id FROM agent_runs r JOIN agents a ON a.id=r.agent_id WHERE r.status IN ('queued','running') AND r.deadline<CURRENT_TIMESTAMP").fetchall()
        for row in rows:
            with self.database.connect() as connection:
                connection.execute('SELECT id FROM agents WHERE answer_attempt_id=%s AND parent_agent_id IS NULL FOR UPDATE', (row['answer_attempt_id'],))
                expired = connection.execute("UPDATE agent_runs SET status='timed_out',error='执行超时。',finished_at=CURRENT_TIMESTAMP WHERE id=%s AND status IN ('queued','running') AND deadline<CURRENT_TIMESTAMP RETURNING operation_id", (row['id'],)).fetchone()
                if expired:
                    connection.execute('UPDATE cloud_operations SET cancelled=TRUE WHERE id=%s', (expired['operation_id'],))

    def record_sources(self, run_id: UUID, results: list[dict[str, Any]]) -> None:
        """仅登记执行器实际返回的工具来源，模型不能直接构造可信登记。"""
        self.guard(run_id)
        with self.database.connect() as connection:
            self.lock_active(connection, run_id)
            for result in results:
                for item in result.get('results', []):
                    if isinstance(item, dict) and isinstance(item.get('key'), str):
                        connection.execute('INSERT INTO agent_sources(run_id,source_key,payload) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (run_id, item['key'], Jsonb(item)))

    def validate_detail(self, answer_id: UUID) -> None:
        """读取历史任务前核对资料版本，过期或撤销后清除旧结论与缓存。"""
        with self.database.connect() as connection:
            root = connection.execute('SELECT conversation_id,source_snapshot FROM agents WHERE answer_attempt_id=%s AND parent_agent_id IS NULL', (answer_id,)).fetchone()
        if root and root['source_snapshot'] != self.snapshot(root['conversation_id']):
            from general_agent.infrastructure.agent_cleanup import invalidate_agent_material
            with self.database.connect() as connection:
                invalidate_agent_material(connection, [root['conversation_id']], [answer_id])

    def sources(self, parent_run: UUID) -> list[dict[str, Any]]:
        """读取主执行自取或经等待交付的来源，不隐式导入后台未交付来源。"""
        parent = self.info(parent_run)
        with self.database.connect() as connection:
            return [row['payload'] for row in connection.execute('SELECT s.payload FROM agent_sources s JOIN agent_runs r ON r.id=s.run_id JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id=%s AND (r.id=%s OR s.delivered)', (parent['answer_attempt_id'], parent_run)).fetchall()]


class RunCheckpoint:
    """为通用工具图提供执行级缓存，同时保留业务工具所需的来源身份。"""

    def __init__(self, repository: AgentRepository, run_id: UUID, message_id: UUID) -> None:
        """显式绑定根用户消息与本次独立操作，避免用户消息级缓存碰撞。"""
        self.repository, self.run_id, self.message_id = repository, run_id, message_id
        row = repository.info(run_id)
        self.conversation_id = row['conversation_id']
        with repository.database.connect() as connection:
            operation = connection.execute('SELECT purpose FROM cloud_operations WHERE id=%s', (row['operation_id'],)).fetchone()
        assert operation is not None
        self.operation = OperationContext(row['operation_id'], operation['purpose'], self.conversation_id, row['account_id'])
        self.current_call = ''
        self.current_tool = ''
        self.main = row['parent_agent_id'] is None
        self.delivered: set[str] = set()

    def guard(self) -> None:
        """核对根回答、执行终态与来源版本。"""
        self.repository.guard(self.run_id)

    def get(self, key: str) -> Any:
        """读取当前执行的缓存步骤，编排工具的结果由自身重新鉴权。"""
        self.guard()
        with self.repository.database.connect() as connection:
            row = connection.execute('SELECT payload FROM agent_messages WHERE run_id=%s AND step_key=%s', (self.run_id, key)).fetchone()
        if row:
            return row['payload']
        if self.main:
            with self.repository.database.connect() as connection:
                previous = connection.execute('SELECT result FROM memory_native_steps WHERE message_id=%s AND step_key=%s', (self.message_id, key)).fetchone()
            if previous and self.memory_step(key, previous['result']):
                return previous['result']
        return None

    def put(self, key: str, value: Any) -> None:
        """在执行仍有效时保存工具或模型步骤，不持久化私有推理文本。"""
        self.guard()
        if isinstance(value, dict):
            value = {name: item for name, item in value.items() if name != 'reasoning_content'}
        with self.repository.database.connect() as connection:
            self.repository.lock_active(connection, self.run_id)
            connection.execute('INSERT INTO agent_messages VALUES(%s,%s,%s) ON CONFLICT(run_id,step_key) DO UPDATE SET payload=EXCLUDED.payload', (self.run_id, key, Jsonb(value)))
            if self.main and self.memory_step(key, value):
                connection.execute('INSERT INTO memory_native_steps(message_id,step_key,result) VALUES(%s,%s,%s) ON CONFLICT(message_id,step_key) DO UPDATE SET result=EXCLUDED.result', (self.message_id, key, Jsonb(value)))

    def memory_step(self, key: str, value: Any) -> bool:
        """仅延续主智能体的既有记忆副作用幂等事实，子任务与普通检索永不跨回答重放。"""
        memory_tools = {'add_memory', 'search_memories', 'update_memory', 'delete_memory'}
        if key.startswith('tools:round:'):
            calls = value.get('tool_calls', []) if isinstance(value, dict) else []
            return bool(calls) and all(call.get('function', {}).get('name') in memory_tools for call in calls)
        return self.current_tool in memory_tools

    @staticmethod
    def key(phase: str, value: Any) -> str:
        """按阶段与内容生成执行内稳定键，不在键中泄露正文。"""
        return phase + ':' + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
