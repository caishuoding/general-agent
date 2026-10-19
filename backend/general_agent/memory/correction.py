"""按明确记忆身份执行更正，旧值先失效，SDK 部分提交可核对恢复。"""

from typing import Any
from uuid import UUID

from general_agent.infrastructure.operations import OperationContext
from general_agent.memory.mem0_adapter import Mem0ApiError
from general_agent.memory.service import MemoryService
from general_agent.infrastructure.provider import ModelFailure


class MemoryCorrection:
    """管理版本、批准来源与更新日志，正式记忆标识在更正中保持稳定。"""

    def __init__(self, memory: MemoryService) -> None:
        """复用已验证的 Mem0 适配及调用网关，不创建独立云端客户端。"""
        self.memory, self.database = memory, memory.database

    def get(self, identifier: UUID, account_id: UUID | None = None) -> dict[str, Any]:
        """按更新身份取得可恢复的明确批准内容，找不到时不猜测其他条目。"""
        with self.database.connect() as connection:
            row=connection.execute(
                "SELECT u.* FROM memory_updates u JOIN memory_actions a ON a.id=u.action_id "
                "WHERE u.id=%s AND (%s::uuid IS NULL OR a.user_id=%s)",
                (identifier, account_id, account_id),
            ).fetchone()
        if row is None:
            raise LookupError('记忆更正记录不存在。')
        return row

    def apply(self, action_id: UUID, fact: str, expected_version: int, identifier: UUID, operation: OperationContext, source_message_id: UUID | None = None, source_conversation_id: UUID | None = None, cached: dict[str, list[float]] | None = None, account_id: UUID | None = None) -> dict[str, Any]:
        """按版本更新并支持原生已计量向量，空间租约覆盖 SDK 写入及来源核验。"""
        with self.database.connect() as lock:
            lock.autocommit=True
            lock.execute('SELECT pg_advisory_lock(%s)',(action_id.int % (2**63-1),))
            existing=lock.execute('SELECT * FROM memory_updates WHERE id=%s',(identifier,)).fetchone()
            entry=lock.execute('SELECT * FROM memory_actions WHERE id=%s',(action_id,)).fetchone()
            if entry is None or entry['memory_id'] is None or entry['status'] in ('deleting','deleted'):
                raise LookupError('正式记忆条目不存在，不能更正。')
            if account_id is not None and entry.get('user_id') != account_id:
                raise LookupError('正式记忆条目不存在，不能更正。')
            account_id = entry.get('user_id')
            if account_id is None and source_conversation_id is not None:
                account_id = self.memory.account_id_for_conversation(source_conversation_id)
            if account_id is None:
                raise LookupError('记忆条目缺少账号归属。')
            if existing:
                if existing['action_id']!=action_id or existing['fact']!=fact or existing['target_version']!=expected_version+1:
                    raise ValueError('更正标识与原目标或内容不一致。')
                if existing['status']=='complete':
                    self.link_operation(identifier,operation)
                    return existing
                if entry['version']!=existing['target_version']:
                    raise ValueError('该记忆已有较新修改，请刷新后核对。')
            else:
                if entry['version']!=expected_version or entry['status']!='complete':
                    raise ValueError('记忆版本已变化或有未完成更正，请刷新或重试原更正。')
                with lock.transaction():
                    self.memory.guard_source(lock,source_conversation_id,source_message_id,operation)
                    changed=lock.execute("UPDATE memory_actions SET status='pending',version=version+1,error='更正尚未完成，旧事实已停止召回。',updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status='complete' AND version=%s RETURNING id",(action_id,expected_version)).fetchone()
                    if changed is None:
                        raise ValueError('目标记忆已变化，旧批准未提交。')
                    lock.execute("INSERT INTO memory_updates(id,action_id,operation_id,target_version,previous_fact,fact,status,source_kind,source_message_id,source_conversation_id) VALUES(%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)",(identifier,action_id,operation.operation_id,expected_version+1,entry['fact'],fact,'chat' if source_message_id else 'management',source_message_id,source_conversation_id))
                existing=self.get(identifier)
            self.link_operation(identifier,operation)
            try:
                lock.execute("UPDATE memory_updates SET status='saving',operation_id=%s,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status<>'deleted'",(operation.operation_id,identifier))
                lock.execute("UPDATE memory_actions SET status='saving',error='正在同步更正，旧事实已停止召回。' WHERE id=%s AND status NOT IN ('deleting','deleted')",(action_id,))
                with self.memory.session() as sdk:
                    current=sdk.get(str(entry['memory_id']))
                    if current is None or current.get('user_id')!=str(account_id):
                        raise ValueError('原正式记忆不存在或身份不符，未更正。')
                    if current['memory']!=fact or (current.get('metadata') or {}).get('correction_id')!=str(identifier):
                        metadata = {**(current.get('metadata') or {}), 'correction_id': str(identifier)}
                        sdk.update(str(entry['memory_id']),fact,metadata=metadata)
                    current=sdk.get(str(entry['memory_id']))
                    if current is None or current['memory']!=fact:
                        raise ValueError('正式记忆与更正内容不一致，结果待核对。')
                with lock.transaction():
                    self.memory.guard_source(lock,source_conversation_id,source_message_id,operation)
                    changed=lock.execute("UPDATE memory_actions SET fact=%s,status='complete',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status NOT IN ('deleting','deleted') RETURNING id",(fact,action_id)).fetchone()
                    if changed is None:
                        raise ValueError('该长期记忆已删除，迟到的更正未生效。')
                    lock.execute("UPDATE memory_updates SET status='complete',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status<>'deleted'",(identifier,))
            except Mem0ApiError:
                detail = '长期记忆服务暂时不可用，更正未完成；请重试核对已有写入。'
                self.fail(identifier,action_id,'failed',detail)
                raise ValueError(detail) from None
            except Exception as error:
                detail=str(error) if isinstance(error,(ValueError,ModelFailure)) else '记忆更正未完成，请重试核对已有写入；旧事实已停止召回。'
                self.fail(identifier,action_id,'failed',detail)
                raise ValueError(detail) from None
        return self.get(identifier)

    def link_operation(self, identifier: UUID, operation: OperationContext) -> None:
        """保留每个重试操作的关联，后续删除能清除全部幂等缓存中的旧新正文。"""
        with self.database.connect() as connection:
            connection.execute('INSERT INTO memory_update_operations(update_id,operation_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(identifier,operation.operation_id))

    def fail(self, identifier: UUID, action_id: UUID, status: str, detail: str) -> None:
        """记录未完成状态，但绝不覆盖已接受的删除墓碑。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE memory_updates SET status=%s,error=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status<>'deleted'",(status,detail,identifier))
            connection.execute("UPDATE memory_actions SET status=%s,error=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status NOT IN ('deleting','deleted')",(status,detail,action_id))
