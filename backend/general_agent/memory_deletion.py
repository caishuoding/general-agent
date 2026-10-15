"""忘记指定长期记忆，真实 SDK 向量与历史均核对清理后才报告完成。"""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from general_agent.memory import MemoryService
from general_agent.settings import StoredEmbeddingSettings


@dataclass(frozen=True)
class DeletionIntent:
    """明确删除指定事实或标识；空目标始终要求澄清。"""
    target: str = ''
    by_id: bool = False


def deletion_intent(content: str) -> DeletionIntent | None:
    """只认可当前用户独立删除指令，混合写作、否定、批量或歧义对象不获得删除授权。"""
    value=content.strip()
    prefix=r'(?:请)?(?:删除|忘记)(?:这条)?(?:长期)?记忆'
    if not re.match(prefix,value):
        return None
    match=re.fullmatch(prefix+r'\s+([0-9a-fA-F-]{36})',value)
    if match:
        try:
            return DeletionIntent(str(UUID(match.group(1))),True)
        except ValueError:
            return DeletionIntent()
    match=re.fullmatch(prefix+r'\s*[：:]\s*(.+)',value)
    if not match or re.search(r'[；;\n？?]|故事|小说|假设|全部|所有|不要|别删除',value):
        return DeletionIntent()
    return DeletionIntent(match.group(1).strip().rstrip('。'))


class MemoryDeletion:
    """将立即停用、稳定标识对账及可恢复的本地清理集中在记忆边界。"""

    def __init__(self, memory: MemoryService) -> None:
        """复用真实 Mem0 与 PostgreSQL，不申请预算或创建云端删除调用。"""
        self.memory,self.database=memory,memory.database

    def result(self, action_id: UUID) -> dict[str, Any]:
        """仅返回不含被删事实的状态回执，内部清理标识不暴露为新的事实副本。"""
        with self.database.connect() as connection:
            row=connection.execute('SELECT action_id,status,error FROM memory_deletions WHERE action_id=%s',(action_id,)).fetchone()
        if row is None:
            raise LookupError('该记忆没有删除记录。')
        return {**row,'detail':'已忘记这条长期记忆；原聊天仍保留。' if row['status']=='complete' else '该记忆已停止召回，清理尚未完成。'}

    def chat(self, content: str, message_id: UUID) -> str:
        """按唯一完整事实或条目标识接受本地删除；原用户消息持久绑定目标以支持失败重试。"""
        intent=deletion_intent(content)
        assert intent is not None
        if not intent.target:
            return '尚未删除。请明确具体记忆，例如“删除记忆：我住在苏州”，或在长期记忆页面选择条目。'
        with self.database.connect() as connection:
            previous=connection.execute('SELECT action_id FROM memory_delete_requests WHERE message_id=%s',(message_id,)).fetchone()
            if previous:
                identifier=previous['action_id']
            else:
                if intent.by_id:
                    rows=connection.execute('SELECT id FROM memory_actions WHERE id=%s',(UUID(intent.target),)).fetchall()
                else:
                    rows=connection.execute("SELECT id FROM memory_actions WHERE fact=%s AND status NOT IN ('deleting','deleted')",(intent.target,)).fetchall()
                if len(rows)!=1:
                    return '未删除：没有找到唯一的完整记忆。请在长期记忆页面核对条目，或提供完整事实。'
                identifier=rows[0]['id']
                connection.execute('INSERT INTO memory_delete_requests(message_id,action_id) VALUES(%s,%s)',(message_id,identifier))
        return str(self.delete(identifier)['detail'])

    def delete(self, action_id: UUID) -> dict[str, Any]:
        """先停用并清除业务正文，再按原身份删除向量和全部历史；失败保留可重试清理状态。"""
        with self.database.connect() as connection:
            entry=connection.execute('SELECT * FROM memory_actions WHERE id=%s FOR UPDATE',(action_id,)).fetchone()
            if entry is None:
                raise LookupError('记忆条目不存在。')
            if entry['status']=='deleted':
                return self.result(action_id)
            connection.execute("INSERT INTO memory_deletions(action_id,status,memory_ids) VALUES(%s,'pending',%s) ON CONFLICT(action_id) DO UPDATE SET status='pending',error=NULL,updated_at=CURRENT_TIMESTAMP",(action_id,[entry['memory_id']] if entry['memory_id'] else []))
            connection.execute("UPDATE memory_actions SET status='deleting',fact='',version=version+CASE WHEN status='deleting' THEN 0 ELSE 1 END,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(action_id,))
            self.clear_dependents(connection,action_id)
        with self.database.connect() as lock:
            lock.autocommit=True
            lock.execute('SELECT pg_advisory_lock(%s)',(action_id.int % (2**63-1),))
            try:
                with lock.transaction():
                    self.clear_dependents(lock,action_id)
                self.purge_sdk(action_id)
                with lock.transaction():
                    lock.execute("UPDATE memory_actions SET status='deleted',memory_id=NULL,fact='',error=NULL WHERE id=%s",(action_id,))
                    lock.execute("UPDATE memory_deletions SET status='complete',memory_ids='{}',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE action_id=%s",(action_id,))
            except Exception:
                lock.execute("UPDATE memory_deletions SET status='failed',error='记忆清理未完成，请重试；该条目仍停止召回。',updated_at=CURRENT_TIMESTAMP WHERE action_id=%s",(action_id,))
                raise ValueError('记忆清理未完成，请重试；该条目仍停止召回。') from None
        return self.result(action_id)

    def clear_dependents(self, connection: Any, action_id: UUID) -> None:
        """在停用事务内清正文、全部旧许可及派生结果；同源其他事实保持独立。"""
        action=connection.execute('SELECT message_id,operation_id FROM memory_actions WHERE id=%s',(action_id,)).fetchone()
        updates=connection.execute('SELECT * FROM memory_updates WHERE action_id=%s',(action_id,)).fetchall()
        sources={action['message_id']} | {row['source_message_id'] for row in updates if row['source_message_id']}
        candidates=connection.execute('SELECT id,target_id,memory_action_id,source_message_id FROM memory_candidates WHERE source_message_id=ANY(%s) OR target_id=%s OR memory_action_id=%s',(list(sources),action_id,action_id)).fetchall()
        affected=[row['id'] for row in candidates if row['target_id']==action_id or row['memory_action_id']==action_id or uuid5(row['id'],'confirmed-memory')==action_id]
        sources.update(row['source_message_id'] for row in candidates if row['id'] in affected)
        operations={action['operation_id']} | {row['operation_id'] for row in updates}
        for table,column,identifiers in [('memory_update_operations','update_id',[row['id'] for row in updates]),('memory_candidate_operations','candidate_id',affected),('memory_discovery_operations','message_id',list(sources))]:
            operations.update(row['operation_id'] for row in connection.execute(f'SELECT operation_id FROM {table} WHERE {column}=ANY(%s)',(identifiers,)).fetchall())
        operations.update(row['operation_id'] for row in connection.execute('SELECT operation_id FROM messages WHERE reply_to=ANY(%s) AND operation_id IS NOT NULL',(list(sources),)).fetchall())
        recalls=connection.execute('SELECT operation_id,matches FROM memory_recalls WHERE matches @> %s',(Jsonb([{'action_id':str(action_id)}]),)).fetchall()
        operations.update(row['operation_id'] for row in recalls)
        searches=connection.execute("SELECT operation_id FROM search_requests WHERE sources @> %s",(Jsonb([{'kind':'memory','id':str(action_id)}]),)).fetchall()
        operations.update(row['operation_id'] for row in searches)
        jobs=[row['document_job_id'] for row in connection.execute('SELECT DISTINCT document_job_id FROM messages WHERE operation_id=ANY(%s) AND document_job_id IS NOT NULL',(list(operations),)).fetchall()]
        operations.update(row['operation_id'] for row in connection.execute('SELECT operation_id FROM messages WHERE document_job_id=ANY(%s) AND operation_id IS NOT NULL',(jobs,)).fetchall())
        connection.execute("UPDATE cloud_operations SET cancelled=TRUE,invalidated=TRUE,approved=FALSE,result=NULL,response_status=NULL,pause_id=NULL,reason=NULL WHERE id=ANY(%s)",(list(operations),))
        connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE operation_id=ANY(%s) AND status='reserved'",(list(operations),))
        connection.execute("UPDATE messages SET status='stopped',budget_pause=NULL,error='相关长期记忆已删除，本次操作已失效。' WHERE operation_id=ANY(%s) AND status IN ('generating','paused')",(list(operations),))
        connection.execute("UPDATE search_requests SET status='expired',query='',results='[]',sources='[]',purpose='',expires_at=0,error='相关记忆已删除，旧搜索批准已失效。' WHERE operation_id=ANY(%s)",(list(operations),))
        connection.execute("UPDATE messages SET web_sources='[]' WHERE operation_id=ANY(%s)",(list(operations),))
        connection.execute("UPDATE messages SET citations='[]',document_progress=NULL WHERE document_job_id=ANY(%s)",(jobs,))
        connection.execute("UPDATE document_jobs SET final_content=NULL,final_call_id=NULL,context_hash='' WHERE id=ANY(%s)",(jobs,))
        for row in recalls:
            matches=[item for item in row['matches'] if item['action_id']!=str(action_id)]
            connection.execute("UPDATE memory_recalls SET matches=%s,match_count=%s,detail='相关记忆已删除，旧回答许可已失效。' WHERE operation_id=%s",(Jsonb(matches),len(matches),row['operation_id']))
        for source in sources:
            connection.execute('INSERT INTO memory_source_suppressions(message_id) VALUES(%s) ON CONFLICT DO NOTHING',(source,))
        connection.execute("UPDATE memory_updates SET status='deleted',fact='',previous_fact='',error=NULL WHERE action_id=%s",(action_id,))
        connection.execute("UPDATE memory_candidates SET status='expired',fact='',source_quote='',target_fact=NULL,approved_at=NULL,error='相关记忆已删除，旧候选已失效。' WHERE id=ANY(%s)",(affected,))
        # 附件候选由后续迁移提供；只有存在时才清对应候选派生来源，绝不删除附件本体。
        if connection.execute("SELECT to_regclass('attachment_memory_candidates') AS name").fetchone()['name']:
            connection.execute('DELETE FROM attachment_memory_candidates WHERE candidate_id=ANY(%s)',(affected,))

    def purge_sdk(self, action_id: UUID) -> None:
        """使用持久化索引维度打开无云调用权限的 SDK，核对部分提交标识并逐条清除历史。"""
        with self.database.connect() as connection:
            index=connection.execute("SELECT base_url,model,dimensions FROM embedding_indexes WHERE name='personal_memories'").fetchone()
            progress=connection.execute('SELECT memory_ids FROM memory_deletions WHERE action_id=%s',(action_id,)).fetchone()
        assert progress is not None
        if index is None:
            if progress['memory_ids']:
                raise ValueError('记忆索引身份无法核验。')
            return
        settings=StoredEmbeddingSettings(**index,api_key='')
        with self.memory.session(settings) as sdk:
            rows=sdk.vector_store.list(filters={'action_id':str(action_id),'user_id':'local-person'},limit=1000)[0]
            identifiers=set(progress['memory_ids']) | {UUID(str(row.id)) for row in rows}
            with self.database.connect() as connection:
                connection.execute('UPDATE memory_deletions SET memory_ids=%s WHERE action_id=%s',(list(identifiers),action_id))
            for identifier in identifiers:
                entry=sdk.get(str(identifier))
                if entry is not None:
                    if entry.get('user_id')!='local-person' or (entry.get('metadata') or {}).get('action_id')!=str(action_id):
                        raise ValueError('记忆身份不符，未删除其他条目。')
                    sdk.delete(str(identifier))
                sdk.db.purge(str(identifier))
                if sdk.get(str(identifier)) is not None or sdk.history(str(identifier)):
                    raise ValueError('正式记忆或内部历史尚未清理。')
            if sdk.vector_store.list(filters={'action_id':str(action_id),'user_id':'local-person'},limit=1)[0]:
                raise ValueError('仍有待清理的同一动作实体，请继续重试。')
