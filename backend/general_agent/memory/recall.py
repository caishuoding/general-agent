"""相关长期记忆召回；持久化检查点仅保留标识和版本，不复制事实正文。"""

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from general_agent.infrastructure.operations import OperationContext
from general_agent.memory.service import MemoryService
from general_agent.infrastructure.provider import ModelFailure, ModelMessage
from general_agent.prompts import memory_data, memory_failure

LIMIT = 5


class MemoryRecall:
    """通过真实 Mem0 搜索取得相关结果，并由应用批准状态决定是否可用。"""

    def __init__(self, memory: MemoryService) -> None:
        """共用正式记忆模块的数据库、嵌入配置与统一调用边界。"""
        self.memory, self.database = memory, memory.database

    def available(self) -> bool:
        """只把业务完整成功且仍有正式标识的记忆视作可召回候选。"""
        with self.database.connect() as connection:
            return connection.execute("SELECT id FROM memory_actions WHERE status='complete' AND memory_id IS NOT NULL LIMIT 1").fetchone() is not None

    def checkpoint(self, operation: OperationContext) -> dict[str, Any] | None:
        """读取同一回答尝试的持久化召回进度，恢复执行不会再次查询向量。"""
        with self.database.connect() as connection:
            return connection.execute('SELECT * FROM memory_recalls WHERE operation_id=%s',(operation.operation_id,)).fetchone()

    def record(self, operation: OperationContext, user_id: UUID, status: str, detail: str, matches: list[dict[str, Any]] | None = None, revision: str | None = None) -> None:
        """只存标识和版本；与删除原子核对有效事实及取消状态，避免迟到材料绕过失效。"""
        hits=matches or []
        with self.database.connect() as connection:
            for match in hits:
                valid=connection.execute("SELECT id FROM memory_actions WHERE id=%s AND memory_id=%s AND version=%s AND status='complete' FOR SHARE",(match['action_id'],match['memory_id'],match['version'])).fetchone()
                if valid is None:
                    raise ValueError('召回的记忆已变化，旧材料不能继续使用。')
            active=connection.execute('SELECT cancelled FROM cloud_operations WHERE id=%s FOR UPDATE',(operation.operation_id,)).fetchone()
            if active is None or active['cancelled']:
                raise ValueError('原操作已失效，未保存迟到的召回检查点。')
            connection.execute('INSERT INTO memory_recalls(operation_id,message_id,status,matches,match_count,detail,embedding_revision) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(operation_id) DO UPDATE SET status=EXCLUDED.status,matches=EXCLUDED.matches,match_count=EXCLUDED.match_count,detail=EXCLUDED.detail,embedding_revision=EXCLUDED.embedding_revision,updated_at=CURRENT_TIMESTAMP',(operation.operation_id,user_id,status,Jsonb(hits),len(hits),detail,revision))

    def prepare(self, user_id: UUID, query: str, operation: OperationContext, reserved: UUID | None, skip: bool = False) -> list[ModelMessage]:
        """空间租约覆盖召回检查点发布，维护期诚实降级而不把新向量用于旧表。"""
        return self.prepare_leased(user_id,query,operation,reserved,skip)

    def prepare_leased(self, user_id: UUID, query: str, operation: OperationContext, reserved: UUID | None, skip: bool = False) -> list[ModelMessage]:
        """召回或重读已完成标识；失败时明确降级当前会话回答并保留已完成步骤。"""
        previous=self.checkpoint(operation)
        if previous and previous['status'] in ('failed','skipped','unavailable'):
            return self.failure_material(previous['detail']) if previous['status'] != 'skipped' else []
        if previous and previous['status']=='complete':
            try:
                return self.materialize(operation,user_id,previous['matches'],previous['embedding_revision'])
            except Exception:
                detail='长期记忆重新核验失败，本次仅按当前会话回答。'
                self.record(operation,user_id,'failed',detail)
                return self.failure_material(detail)
        if skip or not self.available():
            self.record(operation,user_id,'skipped','当前没有需要召回的已批准长期记忆。')
            return []
        self.record(operation,user_id,'pending','正在检索相关长期记忆。')
        try:
            with self.memory.session() as memory:
                found=memory._results(memory.search(query,user_id=self.memory.user_id,limit=50,filters={'user_id':self.memory.user_id}))
            matches=[]
            with self.database.connect() as connection:
                for hit in found:
                    row=connection.execute("SELECT id,memory_id,version,fact FROM memory_actions WHERE memory_id=%s AND status='complete'",(hit['id'],)).fetchone()
                    if row and row['fact']==hit['memory']:
                        matches.append({'memory_id':str(row['memory_id']),'action_id':str(row['id']),'version':row['version']})
                        if len(matches)==LIMIT:
                            break
            self.record(operation,user_id,'complete','已完成相关记忆检索。',matches)
            return self.materialize(operation,user_id,matches,None)
        except Exception as error:
            detail='长期记忆召回失败，本次仅按当前会话回答。'
            if isinstance(error,ModelFailure):
                detail+=' '+str(error)
            self.record(operation,user_id,'failed',detail)
            return self.failure_material(detail)

    def materialize(self, operation: OperationContext, user_id: UUID, matches: list[dict[str, Any]], revision: str | None) -> list[ModelMessage]:
        """每次发回答前重核有效状态和版本，再读取正式正文；变更条目不沿旧检查点使用。"""
        if not matches:
            return []
        valid=[]
        facts=[]
        with self.memory.session() as memory:
            for match in matches:
                with self.database.connect() as connection:
                    row=connection.execute("SELECT fact FROM memory_actions WHERE id=%s AND memory_id=%s AND version=%s AND status='complete'",(match['action_id'],match['memory_id'],match['version'])).fetchone()
                if row is None:
                    continue
                item=memory.get(match['memory_id'])
                if item is None or item.get('user_id')!=self.memory.user_id or item['memory']!=row['fact']:
                    continue
                valid.append(match)
                facts.append({'id':match['memory_id'],'fact':row['fact']})
        detail=f'已参考 {len(valid)} 条相关长期记忆。' if valid else '未找到仍然有效的相关长期记忆。'
        self.record(operation,user_id,'complete',detail,valid,revision)
        if not facts:
            return []
        return [{'role':'user','content':memory_data(facts)}]

    @staticmethod
    def failure_material(detail: str) -> list[ModelMessage]:
        """把真实召回失败作为 user 数据告知回答模型，禁止声称已查到长期记忆。"""
        return [{'role':'user','content':memory_failure(detail)}]
