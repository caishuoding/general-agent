"""忘记指定长期记忆，真实 SDK 向量与历史均核对清理后才报告完成。"""

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from general_agent.memory.service import MemoryService


class MemoryDeletion:
    """将立即停用、稳定标识对账及可恢复的本地清理集中在记忆边界。"""

    def __init__(self, memory: MemoryService) -> None:
        """复用真实 Mem0 与 PostgreSQL，不创建云端删除调用。"""
        self.memory,self.database=memory,memory.database

    def result(self, action_id: UUID) -> dict[str, Any]:
        """仅返回不含被删事实的状态回执，内部清理标识不暴露为新的事实副本。"""
        with self.database.connect() as connection:
            row=connection.execute('SELECT action_id,status,error FROM memory_deletions WHERE action_id=%s',(action_id,)).fetchone()
        if row is None:
            raise LookupError('该记忆没有删除记录。')
        return {**row,'detail':'处理完成。' if row['status']=='complete' else '处理尚未完成。'}

    def delete(self, action_id: UUID, active_operation: UUID | None = None, expected_version: int | None = None, account_id: UUID | None = None) -> dict[str, Any]:
        """停用并清全部副本；原生或工具删除保留当前执行身份，其他旧操作全部失效。"""
        with self.database.connect() as connection:
            if not self.begin(connection,action_id,active_operation,expected_version,account_id):
                return self.result(action_id)
        with self.database.connect() as lock:
            lock.autocommit=True
            lock.execute('SELECT pg_advisory_lock(%s)',(action_id.int % (2**63-1),))
            try:
                with lock.transaction():
                    self.clear_dependents(lock,action_id,active_operation)
                self.purge_sdk(action_id)
                with lock.transaction():
                    lock.execute("UPDATE memory_actions SET status='deleted',memory_id=NULL,fact='',error=NULL WHERE id=%s",(action_id,))
                    lock.execute("UPDATE memory_deletions SET status='complete',memory_ids='{}',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE action_id=%s",(action_id,))
            except Exception:
                lock.execute("UPDATE memory_deletions SET status='failed',error='记忆清理未完成，请重试；该条目仍停止召回。',updated_at=CURRENT_TIMESTAMP WHERE action_id=%s",(action_id,))
                raise ValueError('记忆清理未完成，请重试；该条目仍停止召回。') from None
        return self.result(action_id)

    def begin(self, connection: Any, action_id: UUID, active_operation: UUID | None = None, expected_version: int | None = None, account_id: UUID | None = None) -> bool:
        """事务内停用并清派生正文，允许发起删除的当前工具回合返回真实结果。"""
        entry=connection.execute('SELECT * FROM memory_actions WHERE id=%s FOR UPDATE',(action_id,)).fetchone()
        if entry is None:
            raise LookupError('记忆条目不存在。')
        if account_id is not None and entry.get('user_id') != account_id:
            raise LookupError('记忆条目不存在。')
        if entry['status']=='deleted':
            return False
        if expected_version is not None and entry['status'] != 'deleting' and entry['version'] != expected_version:
            raise ValueError('记忆版本已变化，未删除；请重新查找具体条目。')
        connection.execute("INSERT INTO memory_deletions(action_id,status,memory_ids) VALUES(%s,'pending',%s) ON CONFLICT(action_id) DO UPDATE SET status='pending',error=NULL,updated_at=CURRENT_TIMESTAMP",(action_id,[entry['memory_id']] if entry['memory_id'] else []))
        connection.execute("UPDATE memory_actions SET status='deleting',fact='',version=version+CASE WHEN status='deleting' THEN 0 ELSE 1 END,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(action_id,))
        self.clear_dependents(connection,action_id,active_operation)
        return True

    def clear_dependents(self, connection: Any, action_id: UUID, active_operation: UUID | None = None) -> None:
        """清正文、旧许可及原生缓存，原来源标记为已忘记；当前删除回合仅保留执行身份。"""
        action=connection.execute('SELECT message_id,operation_id FROM memory_actions WHERE id=%s',(action_id,)).fetchone()
        updates=connection.execute('SELECT * FROM memory_updates WHERE action_id=%s',(action_id,)).fetchall()
        sources=({action['message_id']} if action['message_id'] else set()) | {row['source_message_id'] for row in updates if row['source_message_id']}
        operations={action['operation_id']} | {row['operation_id'] for row in updates}
        for table,column,identifiers in [('memory_update_operations','update_id',[row['id'] for row in updates])]:
            operations.update(row['operation_id'] for row in connection.execute(f'SELECT operation_id FROM {table} WHERE {column}=ANY(%s)',(identifiers,)).fetchall())
        operations.update(row['operation_id'] for row in connection.execute('SELECT operation_id FROM messages WHERE reply_to=ANY(%s) AND operation_id IS NOT NULL',(list(sources),)).fetchall())
        recalls=connection.execute('SELECT operation_id,matches FROM memory_recalls WHERE matches @> %s',(Jsonb([{'action_id':str(action_id)}]),)).fetchall()
        operations.update(row['operation_id'] for row in recalls)
        searches=connection.execute("SELECT operation_id FROM search_requests WHERE sources @> %s",(Jsonb([{'kind':'memory','id':str(action_id)}]),)).fetchall()
        operations.update(row['operation_id'] for row in searches)
        observed = connection.execute('SELECT message_id FROM memory_tool_observations WHERE action_id=%s',(action_id,)).fetchall()
        observed_sources = sources | {row['message_id'] for row in observed}
        operations.update(row['operation_id'] for row in connection.execute('SELECT operation_id FROM memory_tool_runs WHERE message_id=ANY(%s)',(list(observed_sources),)).fetchall())
        operations.update(row['operation_id'] for row in connection.execute('SELECT operation_id FROM memory_native_runs WHERE message_id=ANY(%s)',(list(observed_sources),)).fetchall())
        connection.execute('DELETE FROM memory_native_steps WHERE message_id=ANY(%s)',(list(observed_sources),))
        connection.execute("UPDATE memory_native_runs SET status='skipped',detail='该来源已停止后续处理。' WHERE message_id=ANY(%s)",(list(sources),))
        connection.execute("UPDATE memory_tool_runs SET result='{}' WHERE message_id=ANY(%s)",(list(observed_sources),))
        operations.discard(active_operation)
        connection.execute("UPDATE cloud_operations SET cancelled=TRUE,invalidated=TRUE,result=NULL,response_status=NULL WHERE id=ANY(%s)",(list(operations),))
        connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE operation_id=ANY(%s) AND status='reserved'",(list(operations),))
        connection.execute("UPDATE messages SET status='stopped',error='相关长期记忆已删除，本次操作已失效。' WHERE operation_id=ANY(%s) AND status='generating'",(list(operations),))
        connection.execute("UPDATE search_requests SET status='expired',query='',results='[]',sources='[]',purpose='',expires_at=0,error='相关记忆已删除，旧搜索批准已失效。' WHERE operation_id=ANY(%s)",(list(operations),))
        connection.execute("UPDATE messages SET web_sources='[]' WHERE operation_id=ANY(%s)",(list(operations),))
        for row in recalls:
            matches=[item for item in row['matches'] if item['action_id']!=str(action_id)]
            connection.execute("UPDATE memory_recalls SET matches=%s,match_count=%s,detail='相关记忆已删除，旧回答许可已失效。' WHERE operation_id=%s",(Jsonb(matches),len(matches),row['operation_id']))
        for source in sources:
            connection.execute('INSERT INTO memory_source_suppressions(message_id) VALUES(%s) ON CONFLICT DO NOTHING',(source,))
        connection.execute("UPDATE memory_updates SET status='deleted',fact='',previous_fact='',error=NULL WHERE action_id=%s",(action_id,))

    def purge_sdk(self, action_id: UUID) -> None:
        """通过官方 Mem0 API 删除该业务动作对应的正式记忆。"""
        with self.database.connect() as connection:
            progress=connection.execute(
                "SELECT d.memory_ids, a.user_id FROM memory_deletions d "
                "JOIN memory_actions a ON a.id=d.action_id WHERE d.action_id=%s",
                (action_id,),
            ).fetchone()
        assert progress is not None
        account_id = progress['user_id']
        if account_id is None:
            raise ValueError('记忆条目缺少账号归属，未执行删除。')
        identifiers=set(progress['memory_ids'])
        with self.memory.session() as sdk:
            for identifier in identifiers:
                entry=sdk.get(str(identifier))
                if entry is not None:
                    if entry.get('user_id')!=str(account_id) or (entry.get('metadata') or {}).get('action_id')!=str(action_id):
                        raise ValueError('记忆身份不符，未删除其他条目。')
                    sdk.delete(str(identifier))
                if sdk.get(str(identifier)) is not None:
                    raise ValueError('正式记忆尚未清理。')
