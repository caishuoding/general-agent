"""按明确记忆身份执行更正，旧值先失效，SDK 部分提交可核对恢复。"""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from general_agent.budget import BudgetPaused, OperationContext
from general_agent.memory import MemoryService
from general_agent.provider import ModelFailure


@dataclass(frozen=True)
class CorrectionIntent:
    """当前输入中明确指定的旧事实或条目标识；空目标表示必须澄清。"""
    target: str = ''
    fact: str = ''
    by_id: bool = False


def correction_intent(content: str) -> CorrectionIntent | None:
    """仅识别当前用户独立更正指令，不从历史、资料或自由模型输出获得授权。"""
    value=content.strip()
    prefix=r'(?:请)?(?:更正|修改|更新)(?:长期)?记忆'
    if not re.match(prefix,value) and not re.match(r'(?:请)?(?:把|将)(?:长期)?记忆中',value):
        return None
    if re.search(r'[；;\n]|(?:写|创作|编|虚构).{0,12}(?:故事|小说|角色|台词)|假设',value):
        return CorrectionIntent()
    identified=re.fullmatch(prefix+r'\s+([0-9a-fA-F-]{36})\s*[：:]\s*(.+)',value)
    quoted=re.fullmatch(r'(?:请)?(?:把|将)(?:长期)?记忆中的[“"](.+?)[”"](?:改为|更正为)[“"](.+)[”"]',value)
    pair=re.fullmatch(prefix+r'\s*[：:]\s*(.+?)\s*(?:=>|→)\s*(.+)',value)
    match=identified or quoted or pair
    if match is None:
        return CorrectionIntent()
    target,fact=(part.strip() for part in match.groups())
    if not target or not fact or len(fact)>5000 or re.search(r'[？?]',fact):
        return CorrectionIntent()
    return CorrectionIntent(target,fact,identified is not None)


class MemoryCorrection:
    """管理版本、批准来源与更新日志，正式记忆标识在更正中保持稳定。"""

    def __init__(self, memory: MemoryService) -> None:
        """复用已验证的 Mem0 适配及预算网关，不创建独立云端客户端。"""
        self.memory, self.database = memory, memory.database

    def get(self, identifier: UUID) -> dict[str, Any]:
        """按更新身份取得可恢复的明确批准内容，找不到时不猜测其他条目。"""
        with self.database.connect() as connection:
            row=connection.execute('SELECT * FROM memory_updates WHERE id=%s',(identifier,)).fetchone()
        if row is None:
            raise LookupError('记忆更正记录不存在。')
        return row

    def chat(self, content: str, message_id: UUID, conversation_id: UUID, operation: OperationContext) -> str:
        """只恢复原消息的明确更正身份；候选更正独立，目标仍须明确标识或唯一完整旧事实。"""
        intent=correction_intent(content)
        assert intent is not None
        if not intent.target:
            return '请指定具体记忆和新事实，例如“更正记忆：我住在杭州 => 我住在苏州”，或在长期记忆页面编辑。'
        with self.database.connect() as connection:
            previous=connection.execute('SELECT * FROM memory_updates WHERE id=%s',(uuid5(message_id,'explicit-correction:0'),)).fetchone()
            if previous:
                entry_id,version=previous['action_id'],previous['target_version']-1
            else:
                if intent.by_id:
                    rows=connection.execute("SELECT id,version FROM memory_actions WHERE id=%s AND memory_id IS NOT NULL AND status='complete'",(UUID(intent.target),)).fetchall()
                else:
                    rows=connection.execute("SELECT id,version FROM memory_actions WHERE fact=%s AND memory_id IS NOT NULL AND status='complete'",(intent.target,)).fetchall()
                if len(rows)!=1:
                    return '未找到唯一、当前有效的具体记忆，尚未更正。请在长期记忆页面选择条目，或提供完整旧事实和新事实。'
                entry_id,version=rows[0]['id'],rows[0]['version']
        result=self.apply(entry_id,intent.fact,version,uuid5(message_id,'explicit-correction:0'),operation,message_id,conversation_id)
        return '已更正长期记忆：'+str(result['fact'])

    def apply(self, action_id: UUID, fact: str, expected_version: int, identifier: UUID, operation: OperationContext, source_message_id: UUID | None = None, source_conversation_id: UUID | None = None) -> dict[str, Any]:
        """先取得空间租约并拒绝维护期写入，再接受新版本；租约覆盖 SDK 与成功发布。"""
        with self.memory.embedding.spaces.lease(), self.database.connect() as lock:
            lock.autocommit=True
            lock.execute('SELECT pg_advisory_lock(%s)',(action_id.int % (2**63-1),))
            existing=lock.execute('SELECT * FROM memory_updates WHERE id=%s',(identifier,)).fetchone()
            entry=lock.execute('SELECT * FROM memory_actions WHERE id=%s',(action_id,)).fetchone()
            if entry is None or entry['memory_id'] is None or entry['status'] in ('deleting','deleted'):
                raise LookupError('正式记忆条目不存在，不能更正。')
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
                settings=self.memory.embedding.verified(leased=True)
                with self.memory.session(settings,operation) as sdk:
                    current=sdk.get(str(entry['memory_id']))
                    if current is None or current.get('user_id')!='local-person':
                        raise ValueError('原正式记忆不存在或身份不符，未更正。')
                    if current['memory']!=fact or (current.get('metadata') or {}).get('correction_id')!=str(identifier):
                        sdk.update(str(entry['memory_id']),fact,metadata={'correction_id':str(identifier)})
                    current=sdk.get(str(entry['memory_id']))
                    if current is None or current['memory']!=fact:
                        raise ValueError('正式记忆与更正内容不一致，结果待核对。')
                    history=sdk.history(str(entry['memory_id']))
                    if not any(item['event']=='UPDATE' and item['new_memory']==fact and item['old_memory']==existing['previous_fact'] and item['updated_at']==current.get('updated_at') for item in history):
                        sdk.db.add_history(str(entry['memory_id']),existing['previous_fact'],fact,'UPDATE',created_at=current['created_at'],updated_at=current.get('updated_at'),role='user')
                with lock.transaction():
                    self.memory.guard_source(lock,source_conversation_id,source_message_id,operation)
                    changed=lock.execute("UPDATE memory_actions SET fact=%s,status='complete',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status NOT IN ('deleting','deleted') RETURNING id",(fact,action_id)).fetchone()
                    if changed is None:
                        raise ValueError('该长期记忆已删除，迟到的更正未生效。')
                    lock.execute("UPDATE memory_updates SET status='complete',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status<>'deleted'",(identifier,))
            except BudgetPaused:
                self.fail(identifier,action_id,'pending','等待预算许可，更正未完成；旧事实已停止召回。')
                raise
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
