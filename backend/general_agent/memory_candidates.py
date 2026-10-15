"""只从当前用户个人事实提出候选；明确确认之前不写入正式 Mem0。"""

import json
import re
from typing import Any
from uuid import UUID, uuid5

from general_agent.attachment_candidate_source import validate_attachment_source
from general_agent.budget import BudgetPaused, OperationContext
from general_agent.memory import MemoryService
from general_agent.memory_correction import MemoryCorrection
from general_agent.provider import ModelFailure
from general_agent.settings import StoredSettings


def candidate_eligible(content: str) -> bool:
    """保守识别当前个人事实，问题、转述、资料与动作指令不自动触发候选模型。"""
    return bool(re.match(r'^\s*(?:我(?:现在|最近)?(?:喜欢|偏好|住在|居住在|在|是)|我的(?:名字|昵称|职业|工作|生日|语言)|最近在)',content)) and not re.search(r'记住|记忆|更正|更新|修改|查|搜|附件|文件|文档|总结|翻译|小说|故事|假设|不要|别|不想|朋友|客户|别人|[“”"？?]|怎么|如何|哪里|哪个|是否|吗',content)


class MemoryCandidates:
    """候选发现、来源核验与确认提交使用同一个持久化授权边界。"""

    def __init__(self, memory: MemoryService) -> None:
        """复用正式记忆服务和统一预算调用，不构造默认 SDK 推断客户端。"""
        self.memory,self.database=memory,memory.database

    def source(self, message_id: UUID, conversation_id: UUID) -> dict[str, Any]:
        """只读取未停用会话的原用户消息，来源删除中不继续外发或批准。"""
        with self.database.connect() as connection:
            row=connection.execute("SELECT m.id,m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=%s AND m.conversation_id=%s AND m.role='user' AND NOT c.deleting",(message_id,conversation_id)).fetchone()
        if row is None:
            raise ValueError('候选来源已不存在或不是用户消息。')
        return row

    def targets(self, content: str) -> list[dict[str, Any]]:
        """只提供与当前表达同类的少量正式目标，不把全部记忆或旧聊天发给提取模型。"""
        pattern=None
        if re.search(r'住在|居住在|最近在',content):
            pattern=r'^我(现在)?(住在|居住在)'
        elif re.search(r'喜欢|偏好',content):
            pattern=r'^我(喜欢|偏好)'
        else:
            match=re.match(r'^我的(名字|昵称|职业|工作|生日|语言)',content)
            if match:
                pattern='^我的'+match.group(1)
        if pattern is None:
            return []
        with self.database.connect() as connection:
            rows=connection.execute("SELECT id,version,fact FROM memory_actions WHERE status='complete' AND memory_id IS NOT NULL AND fact ~ %s ORDER BY updated_at DESC,id LIMIT 5",(pattern,)).fetchall()
        return [{**row,'id':str(row['id'])} for row in rows]

    def get(self, identifier: UUID) -> dict[str, Any]:
        """读取具体候选及独立附件来源，按服务端时间标记过期，客户端不能延长有效期。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE memory_candidates SET status='expired',error='候选已过期，请重新明确具体事实。' WHERE id=%s AND status NOT IN ('confirmed','rejected','expired') AND expires_at<=CURRENT_TIMESTAMP",(identifier,))
            row=connection.execute('SELECT * FROM memory_candidates WHERE id=%s',(identifier,)).fetchone()
            if row:
                row['attachment_source']=connection.execute('SELECT * FROM attachment_memory_candidates WHERE candidate_id=%s',(identifier,)).fetchone()
        if row is None:
            raise LookupError('候选记忆不存在。')
        return row

    def discovery_state(self, message_id: UUID, operation: OperationContext, status: str, error: str | None = None) -> None:
        """仅为仍存在且未失效操作记录提取状态，来源删除后不重新登记晚到失败正文。"""
        with self.database.connect() as connection:
            active=connection.execute('SELECT invalidated FROM cloud_operations WHERE id=%s FOR SHARE',(operation.operation_id,)).fetchone()
            if active is None or active['invalidated']:
                return
            connection.execute('INSERT INTO memory_discoveries(message_id,operation_id,status,error) VALUES(%s,%s,%s,%s) ON CONFLICT(message_id) DO UPDATE SET operation_id=EXCLUDED.operation_id,status=EXCLUDED.status,error=EXCLUDED.error,updated_at=CURRENT_TIMESTAMP',(message_id,operation.operation_id,status,error))

    def discover(self, conversation_id: UUID, message_id: UUID, settings: StoredSettings, operation: OperationContext) -> None:
        """按来源串行发现候选，重复点击不能同时提取同一条当前用户消息。"""
        with self.database.connect() as lock:
            lock.autocommit=True
            lock.execute('SELECT pg_advisory_lock(%s)',(uuid5(message_id,'candidate-discovery').int % (2**63-1),))
            self.discover_source(conversation_id,message_id,settings,operation)

    def discover_source(self, conversation_id: UUID, message_id: UUID, settings: StoredSettings, operation: OperationContext) -> None:
        """仅发送当前获准事实；旧来源抑制与取消状态核验防止删除后重新提出候选。"""
        if self.memory.source_suppressed(message_id):
            return
        source=self.source(message_id,conversation_id)
        if not candidate_eligible(source['content']):
            return
        with self.database.connect() as connection:
            connection.execute('INSERT INTO memory_discovery_operations(message_id,operation_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(message_id,operation.operation_id))
            previous=connection.execute('SELECT * FROM memory_discoveries WHERE message_id=%s',(message_id,)).fetchone()
        if previous and (previous['status']=='complete' or previous['status']=='failed' and previous['operation_id']==operation.operation_id):
            return
        self.discovery_state(message_id,operation,'pending')
        targets={row['id']:row for row in self.targets(source['content'])}
        try:
            response=self.memory.calls.bind(operation).complete(settings,[{'role':'system','content':'memory_candidate_discovery：仅根据当前用户消息提出值得长期保存的候选，不能使用助手、历史或附件信息。返回 JSON {"candidates":[{"fact":"具体事实","quote":"当前消息中的逐字原文","subject":"user","target_id":null}]}，最多五条；没有合适事实返回空列表。人物不明确时不提取。如果可能更新已提供的 targets，使用该条目的 id 作为 target_id；最近在某地不表示搬家，只能提出待用户确认的具体变化，不可声称已更新。不得编造目标或使用未提供的记忆。候选不表示已保存。'},{'role':'user','content':json.dumps({'current_user_message':source['content'],'targets':list(targets.values())},ensure_ascii=False)}])
            with self.database.connect() as connection:
                active=connection.execute('SELECT cancelled FROM cloud_operations WHERE id=%s',(operation.operation_id,)).fetchone()
            if active is None or active['cancelled']:
                raise ValueError('候选提取已停止，未提交候选。')
            parsed=json.loads(response.content)
            items=parsed['candidates']
            if not isinstance(items,list) or len(items)>5:
                raise ValueError('候选返回格式不符合要求。')
            for item in items:
                if not isinstance(item,dict) or item.get('subject')!='user' or not isinstance(item.get('fact'),str) or not 0<len(item['fact'].strip())<=5000 or not isinstance(item.get('quote'),str) or not item['quote'] or item['quote'] not in source['content'] or not candidate_eligible(item['quote']) or item.get('target_id') is not None and item.get('target_id') not in targets:
                    raise ValueError('候选事实、人物归属或原文引用无法核验。')
            with self.database.connect() as connection:
                self.memory.guard_source(connection,conversation_id,message_id,operation)
                active=connection.execute('SELECT cancelled FROM cloud_operations WHERE id=%s FOR UPDATE',(operation.operation_id,)).fetchone()
                suppressed=connection.execute('SELECT message_id FROM memory_source_suppressions WHERE message_id=%s',(message_id,)).fetchone()
                if active is None or active['cancelled'] or suppressed:
                    raise ValueError('候选提取已停止，未提交候选。')
                for index,item in enumerate(items):
                    target=targets.get(item.get('target_id'))
                    connection.execute("INSERT INTO memory_candidates(id,source_message_id,source_conversation_id,source_quote,fact,subject,target_id,target_version,target_fact,status) VALUES(%s,%s,%s,%s,%s,'user',%s,%s,%s,'pending') ON CONFLICT(id) DO NOTHING",(uuid5(message_id,f'candidate:{index}'),message_id,conversation_id,item['quote'],item['fact'].strip(),target['id'] if target else None,target['version'] if target else None,target['fact'] if target else None))
                connection.execute("UPDATE memory_discoveries SET status='complete',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE message_id=%s",(message_id,))
        except BudgetPaused:
            self.discovery_state(message_id,operation,'pending','候选提取等待预算许可，尚未取得结果。')
            raise
        except Exception as error:
            detail=str(error) if isinstance(error,ModelFailure) else '候选识别未完成，未保存新记忆；可以重试识别。'
            self.discovery_state(message_id,operation,'failed',detail)

    def confirm(self, identifier: UUID, operation: OperationContext) -> dict[str, Any]:
        """空间租约覆盖批准与正式发布，维护门槛在 saving 之前；来源与删除失效优先。"""
        with self.memory.embedding.spaces.lease(), self.database.connect() as lock:
            lock.autocommit=True
            lock.execute('SELECT pg_advisory_lock(%s)',(identifier.int % (2**63-1),))
            row=self.get(identifier)
            lock.execute('INSERT INTO memory_candidate_operations(candidate_id,operation_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(identifier,operation.operation_id))
            if row['status']=='confirmed':
                return row
            if row['status'] in ('rejected','expired'):
                raise ValueError('候选已拒绝或过期，不能继续提交。')
            source=self.source(row['source_message_id'],row['source_conversation_id'])
            if row['source_kind']=='attachment':
                validate_attachment_source(self.database,self.memory.directory,row)
            if row['source_kind']=='message' and row['source_quote'] not in source['content']:
                lock.execute("UPDATE memory_candidates SET status='expired',error='原来源已变化，不能提交旧候选。' WHERE id=%s",(identifier,))
                raise ValueError('原来源已变化，请重新确认具体事实。')
            def validate_source() -> None:
                """每次外部嵌入回传及正式发布前重读附件来源和人物批准，拒绝迟到写入。"""
                current=self.get(identifier)
                validate_attachment_source(self.database,self.memory.directory,current)

            update_id=uuid5(identifier,'confirmed-update')
            if row['target_id'] is not None:
                target=lock.execute('SELECT status,version FROM memory_actions WHERE id=%s',(row['target_id'],)).fetchone()
                progress=lock.execute('SELECT status,target_version FROM memory_updates WHERE id=%s',(update_id,)).fetchone()
                valid=target is not None and ((progress is not None and (progress['status']=='complete' or target['version']==progress['target_version'])) or progress is None and target['status']=='complete' and target['version']==row['target_version'])
                if not valid:
                    lock.execute("UPDATE memory_candidates SET status='expired',error='目标记忆已变化，旧候选未覆盖新事实。' WHERE id=%s",(identifier,))
                    raise ValueError('目标记忆已变化，请重新核对具体事实。')
            accepted=lock.execute("UPDATE memory_candidates SET status='saving',approved_at=coalesce(approved_at,CURRENT_TIMESTAMP),error=NULL WHERE id=%s AND status NOT IN ('expired','rejected') RETURNING id",(identifier,)).fetchone()
            if accepted is None:
                raise ValueError('候选已失效，旧批准不能继续提交。')
            try:
                if row['target_id'] is not None:
                    MemoryCorrection(self.memory).apply(row['target_id'],row['fact'],row['target_version'],update_id,operation,row['source_message_id'],row['source_conversation_id'])
                    action_id=row['target_id']
                else:
                    saved=self.memory.save(row['source_conversation_id'],row['source_message_id'],row['fact'],operation,action_id=uuid5(identifier,'confirmed-memory'),validate_source=validate_source if row['source_kind']=='attachment' else None)
                    action_id=saved['id']
                lock.execute("UPDATE memory_candidates SET status='confirmed',memory_action_id=%s,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND status='saving' RETURNING id",(action_id,identifier)).fetchone()
                if self.get(identifier)['status']!='confirmed':
                    raise ValueError('相关记忆已删除，旧候选已失效。')
            except BudgetPaused:
                lock.execute("UPDATE memory_candidates SET status='paused',error='已确认，提交等待预算许可。' WHERE id=%s AND status='saving'",(identifier,))
                raise
            except Exception as error:
                detail=str(error) if isinstance(error,(ModelFailure,ValueError)) else '候选提交未完成，请重试核对。'
                lock.execute("UPDATE memory_candidates SET status='failed',error=%s WHERE id=%s AND status='saving'",(detail,identifier))
                raise ValueError(detail) from None
        return self.get(identifier)

    def reject(self, identifier: UUID) -> dict[str, Any]:
        """拒绝未批准候选并保持幂等；已经开始正式提交的动作不能伪装成未发生。"""
        with self.database.connect() as lock:
            lock.autocommit=True
            lock.execute('SELECT pg_advisory_lock(%s)',(identifier.int % (2**63-1),))
            row=self.get(identifier)
            if row['status']=='rejected':
                return row
            if row['status']!='pending' or row['approved_at'] is not None:
                raise ValueError('候选已提交、过期或正在处理，不能再拒绝原批准。')
            lock.execute("UPDATE memory_candidates SET status='rejected',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(identifier,))
        return self.get(identifier)
