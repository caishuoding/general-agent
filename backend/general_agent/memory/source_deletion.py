"""集中处理来源影响、明确保留选择与可恢复的文件及关联记忆清理。"""

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from general_agent.memory.deletion import MemoryDeletion
from general_agent.memory.service import MemoryService


class SourceDeletion:
    """来源删除先撤回使用许可，再复用正式记忆删除与本机物理清理。"""

    def __init__(self, memory: MemoryDeletion, memories: MemoryService, delete_graph_threads: Callable[[list[UUID]], None] | None = None) -> None:
        """显式绑定删除与正式记忆核验接口，不依赖删除服务内部的 SDK 或空间配置路径。"""
        self.memory=memory
        self.memories=memories
        self.database=memories.database
        self.directory=memories.directory/'attachments'
        self.delete_graph_threads = delete_graph_threads

    def impact(self, connection: Any, kind: str, conversation_id: UUID, source_id: UUID, account_id: UUID) -> dict[str,Any]:
        """按真实来源关系收集关联动作，经正式记忆接口核验可保留项，不按正文扩大影响。"""
        conversation=connection.execute('SELECT * FROM conversations WHERE id=%s AND user_id=%s AND NOT deleting FOR UPDATE',(conversation_id,account_id)).fetchone()
        if conversation is None:
            raise LookupError('会话不存在或正在删除。')
        files=connection.execute('SELECT * FROM attachments WHERE conversation_id=%s AND (%s OR id=%s) ORDER BY id FOR UPDATE',(conversation_id,kind=='conversation',source_id)).fetchall()
        if kind=='attachment' and (not files or files[0]['status']=='deleting'):
            raise LookupError('附件不存在或正在删除。')
        file_ids=[row['id'] for row in files]
        # 附件现在是会话级上下文；删除单个附件时通过活动操作定位正在读取它的回答。
        message_ids=[row['id'] for row in connection.execute('SELECT id FROM messages WHERE conversation_id=%s',(conversation_id,)).fetchall()] if kind=='conversation' else []
        actions=connection.execute("SELECT * FROM memory_actions WHERE user_id=%s AND status<>'deleted' AND %s AND (conversation_id=%s OR id IN (SELECT action_id FROM memory_updates WHERE source_conversation_id=%s)) ORDER BY id FOR UPDATE",(account_id,kind=='conversation',conversation_id,conversation_id)).fetchall()
        valid=self.memories.retainable(actions)
        fingerprint=[kind,str(source_id),str(conversation_id),[(str(row['id']),row['status'],str(row['created_at'])) for row in files],[(str(row['id']),row['status'],row['version'],row['id'] in valid) for row in actions],sorted(map(str,message_ids))]
        revision=hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()
        return {'kind':kind,'source_id':source_id,'conversation_id':conversation_id,'name':conversation['title'] if kind=='conversation' else files[0]['filename'],'revision':revision,'attachment_ids':file_ids,'message_ids':message_ids,'action_ids':[row['id'] for row in actions],'memories':[{**row,'delete_by_default':True,'can_keep':row['id'] in valid} for row in actions]}

    def preview(self, kind: str, conversation_id: UUID, source_id: UUID, account_id: UUID) -> dict[str,Any]:
        """先取空间租约再锁来源行核验活动 SDK，避免与发布形成反向锁序。"""
        with self.memories.maintenance(), self.database.connect() as connection:
            impact=self.impact(connection,kind,conversation_id,source_id,account_id)
        return {key:impact[key] for key in ['kind','source_id','name','revision','memories']}

    def result(self, identifier: UUID, account_id: UUID) -> dict[str,Any]:
        """返回无正文的阶段结果，物理来源不存在后仍可读取和重试。"""
        with self.database.connect() as connection:
            row=connection.execute('SELECT id,kind,source_id,conversation_id,status,phase,error,created_at FROM source_deletions WHERE id=%s AND user_id=%s',(identifier,account_id)).fetchone()
        if row is None:
            raise LookupError('来源删除记录不存在。')
        return row

    def accept(self, kind: str, conversation_id: UUID, source_id: UUID, revision: str, keep_ids: list[UUID], account_id: UUID) -> dict[str,Any]:
        """空间租约内原子验证、保留与停用；墓碑先提交，再等待动作清理，不持来源行等待云请求。"""
        with self.memories.maintenance(), self.database.connect() as connection:
            connection.execute('SELECT pg_advisory_xact_lock(%s)',(source_id.int % (2**63-1),))
            existing=connection.execute('SELECT * FROM source_deletions WHERE kind=%s AND source_id=%s',(kind,source_id)).fetchone()
            if existing:
                if existing['user_id']!=account_id or existing['conversation_id']!=conversation_id or existing['revision']!=revision or set(existing['keep_ids'])!=set(keep_ids):
                    raise ValueError('该来源已有删除决定，不能使用旧页面修改保留选择。')
                identifier=existing['id']
            else:
                impact=self.impact(connection,kind,conversation_id,source_id,account_id)
                keep=set(keep_ids)
                if impact['revision']!=revision or len(keep)!=len(keep_ids) or not keep<={row['id'] for row in impact['memories'] if row['can_keep']}:
                    raise ValueError('来源或关联记忆已经变化，或保留项尚未完整保存；请重新查看影响预览。')
                identifier=uuid4()
                connection.execute("INSERT INTO source_deletions(id,kind,source_id,conversation_id,user_id,revision,status,phase,attachment_ids,message_ids,action_ids,keep_ids) VALUES(%s,%s,%s,%s,%s,%s,'running','invalidate',%s,%s,%s,%s)",(identifier,kind,source_id,conversation_id,account_id,revision,impact['attachment_ids'],impact['message_ids'],impact['action_ids'],keep_ids))
                connection.execute("UPDATE attachments SET status='deleting' WHERE id=ANY(%s)",(impact['attachment_ids'],))
                if kind=='conversation':
                    connection.execute('UPDATE conversations SET deleting=TRUE WHERE id=%s',(conversation_id,))
                self.invalidate(connection,identifier)
                connection.execute('UPDATE memory_actions SET source_deleted=TRUE WHERE id=ANY(%s)',(keep_ids,))
                for action_id in impact['action_ids']:
                    if action_id not in keep:
                        self.memory.begin(connection,action_id)
        return self.resume(identifier, account_id)

    def invalidate(self, connection: Any, identifier: UUID) -> None:
        """撤回来源关联的旧调用和自动记忆缓存，保留单附件所属会话的已完成正文。"""
        row=connection.execute('SELECT * FROM source_deletions WHERE id=%s',(identifier,)).fetchone()
        message_ids=row['message_ids']
        connection.execute('DELETE FROM memory_native_steps WHERE message_id=ANY(%s)',(message_ids,))
        connection.execute("UPDATE memory_tool_runs SET result='{}' WHERE message_id=ANY(%s)",(message_ids,))
        operations={item['operation_id'] for item in connection.execute('SELECT operation_id FROM messages WHERE (id=ANY(%s) OR reply_to=ANY(%s)) AND operation_id IS NOT NULL',(message_ids,message_ids)).fetchall()}
        if row['kind'] == 'attachment':
            operations.update(item['operation_id'] for item in connection.execute("SELECT operation_id FROM messages WHERE conversation_id=%s AND status='generating' AND operation_id IS NOT NULL",(row['conversation_id'],)).fetchall())
        operations.update(item['operation_id'] for item in connection.execute("SELECT operation_id FROM search_requests s WHERE (%s AND conversation_id=%s) OR EXISTS(SELECT 1 FROM jsonb_array_elements(s.sources) source WHERE source->>'kind'='attachment' AND (source->>'id')::uuid=ANY(%s))",(row['kind']=='conversation',row['conversation_id'],row['attachment_ids'])).fetchall())
        if row['kind']=='conversation':
            operations.update(item['operation_id'] for item in connection.execute('SELECT operation_id FROM memory_update_operations WHERE update_id IN (SELECT id FROM memory_updates WHERE source_conversation_id=%s)',(row['conversation_id'],)).fetchall())
        operations.update(item['operation_id'] for item in connection.execute('SELECT operation_id FROM memory_actions WHERE id=ANY(%s) AND operation_id IS NOT NULL',(row['action_ids'],)).fetchall())
        if row['kind']=='conversation':
            operations.update(item['id'] for item in connection.execute('SELECT id FROM cloud_operations WHERE conversation_id=%s',(row['conversation_id'],)).fetchall())
        defaults=[item for item in row['action_ids'] if item not in row['keep_ids']]
        operations.update(item['operation_id'] for item in connection.execute("SELECT operation_id FROM memory_recalls r WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(r.matches) item WHERE (item->>'action_id')::uuid=ANY(%s))",(defaults,)).fetchall())
        operations.update(item['operation_id'] for item in connection.execute("SELECT operation_id FROM search_requests s WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(s.sources) item WHERE item->>'kind'='memory' AND (item->>'id')::uuid=ANY(%s))",(defaults,)).fetchall())
        operations.update(row['operation_ids'])
        connection.execute('UPDATE source_deletions SET operation_ids=%s WHERE id=%s',(list(operations),identifier))
        connection.execute('UPDATE cloud_operations SET cancelled=TRUE,invalidated=TRUE,result=NULL,response_status=NULL WHERE id=ANY(%s)',(list(operations),))
        connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE operation_id=ANY(%s) AND status='reserved'",(list(operations),))
        connection.execute("UPDATE messages SET status='stopped',content='',error='来源已删除，本次生成已失效。' WHERE operation_id=ANY(%s) AND status='generating'",(list(operations),))
        connection.execute("UPDATE search_requests SET status='expired',query='',results='[]',sources='[]',purpose='',expires_at=0,error='来源已删除，旧搜索批准已失效。' WHERE operation_id=ANY(%s)",(list(operations),))
        connection.execute("UPDATE messages SET web_sources='[]' WHERE operation_id=ANY(%s)",(list(operations),))
        for message_id in message_ids:
            connection.execute('INSERT INTO memory_source_suppressions(message_id) VALUES(%s) ON CONFLICT DO NOTHING',(message_id,))

    def resume(self, identifier: UUID, account_id: UUID) -> dict[str,Any]:
        """同一删除身份串行逐步清理，失败保留真实阶段，已完成步骤重试不会扩展批准范围。"""
        with self.database.connect() as lock:
            lock.autocommit=True
            lock.execute('SELECT pg_advisory_lock(%s)',(identifier.int % (2**63-1),))
            row=lock.execute('SELECT * FROM source_deletions WHERE id=%s AND user_id=%s',(identifier,account_id)).fetchone()
            if row is None:
                raise LookupError('来源删除记录不存在。')
            if row['status']=='complete':
                return self.result(identifier, account_id)
            lock.execute("UPDATE source_deletions SET status='running',phase='memories',error=NULL WHERE id=%s",(identifier,))
            try:
                for action_id in row['action_ids']:
                    if action_id not in row['keep_ids']:
                        self.memory.delete(action_id)
                lock.execute("UPDATE source_deletions SET phase='files' WHERE id=%s",(identifier,))
                for attachment_id in row['attachment_ids']:
                    (self.directory/str(attachment_id)).unlink(missing_ok=True)
                lock.execute("UPDATE source_deletions SET phase='relations' WHERE id=%s",(identifier,))
                with lock.transaction():
                    self.invalidate(lock,identifier)
                    self.cleanup(lock,row)
                    if row['kind'] == 'conversation' and self.delete_graph_threads is not None:
                        # 会话正文删除时同步清掉以助手回答标识保存的 LangGraph 检查点。
                        self.delete_graph_threads(row['message_ids'])
                    lock.execute("UPDATE source_deletions SET status='complete',phase='complete',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(identifier,))
            except Exception:
                lock.execute("UPDATE source_deletions SET status='failed',error='来源已停用，但清理尚未完成；请重试同一删除操作。',updated_at=CURRENT_TIMESTAMP WHERE id=%s",(identifier,))
        return self.result(identifier, account_id)

    def cleanup(self, connection: Any, row: dict[str,Any]) -> None:
        """按外键依赖清理来源正文；保留记忆断开已删来源，用量与最小删除身份继续保存。"""
        connection.execute('UPDATE memory_actions SET source_deleted=TRUE WHERE id=ANY(%s)',(row['keep_ids'],))
        connection.execute('DELETE FROM attachments WHERE id=ANY(%s)',(row['attachment_ids'],))
        if row['kind']!='conversation':
            return
        messages=row['message_ids']
        connection.execute('DELETE FROM memory_recalls WHERE message_id=ANY(%s)',(messages,))
        connection.execute("UPDATE memory_updates SET source_deleted=TRUE,source_message_id=NULL,source_conversation_id=NULL,fact='',previous_fact='',error=NULL WHERE source_conversation_id=%s",(row['conversation_id'],))
        connection.execute('UPDATE memory_actions SET source_deleted=TRUE,message_id=NULL,conversation_id=NULL WHERE conversation_id=%s',(row['conversation_id'],))
        connection.execute('DELETE FROM search_requests WHERE conversation_id=%s',(row['conversation_id'],))
        connection.execute('UPDATE model_calls SET assistant_message_id=NULL WHERE assistant_message_id=ANY(%s)',(messages,))
        connection.execute('UPDATE model_calls SET conversation_id=NULL WHERE conversation_id=%s',(row['conversation_id'],))
        connection.execute('UPDATE cloud_operations SET conversation_id=NULL WHERE conversation_id=%s',(row['conversation_id'],))
        connection.execute('UPDATE messages SET reply_to=NULL WHERE reply_to=ANY(%s)',(messages,))
        connection.execute('DELETE FROM messages WHERE conversation_id=%s',(row['conversation_id'],))
        connection.execute('DELETE FROM conversations WHERE id=%s',(row['conversation_id'],))

    def recover(self) -> None:
        """应用重启只标记未完成清理，不把已停用来源恢复为可使用状态。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE source_deletions SET status='failed',error='应用重启中断了清理，请重试；来源仍不可用。' WHERE status='running'")
