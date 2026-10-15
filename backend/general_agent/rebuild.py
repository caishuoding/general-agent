"""个人长期记忆的逐项重建、预算续跑与可恢复空间发布。"""

import hashlib
from threading import Lock, Thread
from typing import Any
from uuid import UUID, uuid4, uuid5

from psycopg import sql
from psycopg.types.json import Jsonb

from general_agent.budget import BudgetPaused
from general_agent.memory import MemoryService
from general_agent.provider import ModelFailure
from general_agent.settings import EmbeddingSettingsInput, SettingsStore, StoredEmbeddingSettings, StoredSettings


class EmbeddingRebuild:
    """管理独立目标空间；每次外部请求均以持久步骤通过原预算网关。"""

    def __init__(self, memory: MemoryService) -> None:
        """共用业务记忆、注册空间与调用边界，工作线程仅处理本模块任务。"""
        self.memory,self.database=memory,memory.database
        self.embedding,self.spaces=memory.embedding,memory.embedding.spaces
        self.budget=memory.calls.budget
        self.threads: set[Thread]=set()
        self.lock=Lock()

    def private(self, identifier: UUID) -> SettingsStore:
        """固定 UUID 派生私有路径，凭据不进入数据库或公开任务状态。"""
        return SettingsStore(self.memory.directory/'embedding-rebuilds',str(identifier)+'.json')

    def target_settings(self, job: dict[str, Any]) -> StoredEmbeddingSettings:
        """核验私有目标文件仍与接受时的 revision 及空间身份一致。"""
        try:
            settings=StoredEmbeddingSettings.model_validate_json(self.private(job['id']).path.read_text())
        except (OSError,ValueError):
            current=self.embedding.store.read()
            if job['phase']=='cleanup' and current and current.revision==job['target_revision']:
                settings=current
            else:
                raise ValueError('重建目标配置不可读取，请恢复本机配置文件后重试。') from None
        space=self.space(job['target_id'])
        if settings.revision!=job['target_revision'] or (settings.base_url,settings.model,settings.dimensions)!=(space['base_url'],space['model'],space['dimensions']):
            raise ValueError('目标配置已变化，不能挪用原重建批准。')
        return settings

    def space(self, identifier: UUID) -> dict[str, Any]:
        """读取应用已经登记的空间；外部标识不能成为任意 SQL 表名。"""
        with self.database.connect() as connection:
            row=connection.execute('SELECT * FROM embedding_spaces WHERE id=%s',(identifier,)).fetchone()
        if row is None:
            raise ValueError('重建空间登记不存在。')
        return row

    def get(self, identifier: UUID) -> dict[str, Any]:
        """读取内部持久任务；不包含密钥、事实正文或嵌入响应。"""
        with self.database.connect() as connection:
            row=connection.execute('SELECT * FROM embedding_rebuilds WHERE id=%s',(identifier,)).fetchone()
        if row is None:
            raise LookupError('重建任务不存在。')
        return row

    def public(self, identifier: UUID) -> dict[str, Any]:
        """按实际已落目标向量的步骤显示完成数量，已删事实单独计为排除。"""
        job=self.get(identifier)
        with self.database.connect() as connection:
            counts=connection.execute("SELECT count(*) AS total,count(*) FILTER(WHERE status='complete') AS completed,count(*) FILTER(WHERE status='invalidated') AS excluded FROM embedding_rebuild_steps WHERE job_id=%s AND action_id IS NOT NULL",(identifier,)).fetchone()
        target=self.space(job['target_id'])
        active=self.spaces.active()
        return {'id':identifier,'status':job['status'],'phase':job['phase'],'pause':job['pause'],'error':job['error'],'target':{key:target[key] for key in ('model','base_url','dimensions')},'active':{key:active[key] for key in ('model','base_url','dimensions')} if active else None,'queryable':not self.spaces.maintaining(),**(counts or {})}

    def latest(self) -> dict[str, Any] | None:
        """设置页刷新后仍可找到最近的任务及未完成恢复入口。"""
        with self.database.connect() as connection:
            row=connection.execute('SELECT id FROM embedding_rebuilds ORDER BY created_at DESC LIMIT 1').fetchone()
        return self.public(row['id']) if row else None

    def impact(self, body: EmbeddingSettingsInput, chat: StoredSettings | None) -> dict[str, Any]:
        """预览唯一向量集合的影响，不发送探针或把附件、历史改为向量检索。"""
        saved=self.embedding.store.prepare(body,chat)
        active=self.spaces.active()
        with self.database.connect() as connection:
            count=connection.execute("SELECT count(*) AS n FROM memory_actions WHERE status='complete' AND memory_id IS NOT NULL").fetchone()
        changed=bool(active and (active['base_url'],active['model'],active['dimensions'])!=(saved.base_url,saved.model,saved.dimensions))
        return {'requires_rebuild':changed,'memory_count':count['n'] if count else 0,'target':saved.public().model_dump(),'collections':[{'kind':'memory','name':'已批准长期记忆','affected':changed},{'kind':'attachments','name':'附件文字与全文总结：无向量索引','affected':False},{'kind':'images','name':'图片理解：无向量索引','affected':False},{'kind':'history','name':'历史消息本机文字检索：无向量索引','affected':False}]}

    def start(self, identifier: UUID, body: EmbeddingSettingsInput, chat: StoredSettings | None, confirmed: bool) -> dict[str, Any]:
        """明确批准后登记唯一目标和维护门槛；同一请求身份重复受理不重复收费。"""
        if not confirmed:
            raise ValueError('请先核对影响并明确确认重建。')
        self.spaces.registered()
        with self.database.connect() as connection:
            connection.execute('SELECT pg_advisory_xact_lock(91402703)')
            saved=self.embedding.store.prepare(body,chat)
            digest=hashlib.sha256(saved.model_dump_json(exclude={'revision'}).encode()).hexdigest()
            active=connection.execute("SELECT * FROM embedding_spaces WHERE status='active'").fetchone()
            previous=connection.execute('SELECT scope_hash FROM embedding_rebuilds WHERE id=%s',(identifier,)).fetchone()
            if previous:
                if previous['scope_hash']!=digest:
                    raise ValueError('重建标识已经用于其他目标，不能修改原批准范围。')
                connection.commit()
                return self.public(identifier)
            if connection.execute("SELECT id FROM embedding_rebuilds WHERE status NOT IN ('complete','cancelled')").fetchone():
                raise ValueError('已有未完成重建，请先恢复或取消原任务。')
            if active is None or (active['base_url'],active['model'],active['dimensions'])==(saved.base_url,saved.model,saved.dimensions):
                raise ValueError('该目标不需要重建；请直接保存同空间配置。')
            self.private(identifier).write(saved)
            target_id=uuid4()
            connection.execute("INSERT INTO embedding_spaces(id,collection_name,base_url,model,dimensions,status) VALUES(%s,%s,%s,%s,%s,'target')",(target_id,'memory_space_'+target_id.hex,saved.base_url,saved.model,saved.dimensions))
            connection.execute('INSERT INTO embedding_rebuilds(id,scope_hash,target_id,old_id,target_revision) VALUES(%s,%s,%s,%s,%s)',(identifier,digest,target_id,active['id'],saved.revision))
        self.launch(identifier)
        return self.public(identifier)

    def launch(self, identifier: UUID) -> None:
        """启动轻量后台执行；数据库任务锁负责跨请求或进程防止重复工作。"""
        thread=Thread(target=self.execute,args=(identifier,),daemon=True)
        with self.lock:
            self.threads={item for item in self.threads if item.is_alive()}
            self.threads.add(thread)
            thread.start()

    def resume(self, identifier: UUID) -> dict[str, Any]:
        """恢复原任务的目标与范围；完成步骤先对账，暂停挑战仍沿原操作。"""
        job=self.get(identifier)
        if job['status'] not in ('complete','cancelled'):
            self.launch(identifier)
        return self.public(identifier)

    def cancel(self, identifier: UUID) -> dict[str, Any]:
        """撤回目标任务的后续调用；发布提交点之后只允许完成核对与旧表清理。"""
        with self.database.connect() as connection:
            job=connection.execute('SELECT * FROM embedding_rebuilds WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
            if job is None:
                raise LookupError('重建任务不存在。')
            if job['status'] in ('complete','cancelled'):
                connection.commit()
                return self.public(identifier)
            if job['phase'] in ('publishing','cleanup'):
                raise ValueError('目标已进入发布，请继续完成核对及清理，不能回退到旧配置。')
            connection.execute("UPDATE embedding_rebuilds SET status='cancelling',phase='cancelling',pause=NULL,error=NULL WHERE id=%s",(identifier,))
            operations=connection.execute('SELECT o.operation_id FROM embedding_rebuild_operations o JOIN embedding_rebuild_steps s ON s.id=o.step_id WHERE s.job_id=%s',(identifier,)).fetchall()
            ids=[row['operation_id'] for row in operations]
            connection.execute('UPDATE cloud_operations SET cancelled=TRUE,invalidated=TRUE,approved=FALSE,pause_id=NULL,reason=NULL,result=NULL,response_status=NULL WHERE id=ANY(%s)',(ids,))
            connection.execute("UPDATE model_calls SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE operation_id=ANY(%s) AND status='reserved'",(ids,))
        self.launch(identifier)
        return self.public(identifier)

    def finish_cancel(self, job: dict[str, Any]) -> None:
        """等在途目标写入结束后清理仅本任务的目标表和私有文件，原活动空间保持。"""
        with self.spaces.lease(exclusive=True,maintenance=True), self.database.connect() as connection:
            target=self.space(job['target_id'])
            if target['status']=='active':
                raise ValueError('已发布目标不能取消，请继续清理。')
            connection.execute(sql.SQL('DROP TABLE IF EXISTS {}').format(sql.Identifier(target['collection_name'])))
            connection.execute("UPDATE embedding_spaces SET status='removed' WHERE id=%s",(target['id'],))
            connection.execute("UPDATE embedding_rebuild_steps SET status='invalidated',fact_hash=NULL WHERE job_id=%s",(job['id'],))
            self.private(job['id']).path.unlink(missing_ok=True)
            connection.execute("UPDATE embedding_rebuilds SET status='cancelled',phase='cancelled',pause=NULL,error=NULL WHERE id=%s",(job['id'],))

    def recover(self) -> None:
        """重启只标记中断，不自动发云请求；保留已完成步骤和预算暂停许可。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE embedding_rebuilds SET status='failed',error='应用重启中断了重建；已完成步骤保留，请继续原任务。' WHERE status IN ('running','cancelling')")

    def close(self) -> None:
        """正常退出等待已受理线程结束，进程异常恢复由持久步骤负责。"""
        for thread in tuple(self.threads):
            thread.join()

    def change(self, identifier: UUID, status: str, phase: str | None = None, error: str | None = None, pause: dict[str, str] | None = None) -> None:
        """记录真实状态，不用异常文本或私有配置冒充可公开诊断。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE embedding_rebuilds SET status=%s,phase=coalesce(%s,phase),error=%s,pause=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND (status<>'cancelling' OR %s IN ('cancelling','cancelled'))",(status,phase,error,Jsonb(pause) if pause else None,identifier,status))

    def initialize(self, job: dict[str, Any]) -> None:
        """等在途发布后建立有效版本快照；未完成保存/更正保留旧索引，明确保留来源事实仍有效。"""
        with self.spaces.lease(exclusive=True,maintenance=True), self.database.connect() as connection:
            unfinished=connection.execute("SELECT id FROM memory_actions WHERE status IN ('pending','saving','failed') LIMIT 1").fetchone()
            if unfinished:
                raise ValueError('存在未完成的记忆保存或更正。请取消重建后先恢复原操作，或明确忘记该条目，再启动重建；原索引保持。')
            rows=connection.execute("SELECT id,memory_id,version,fact FROM memory_actions WHERE status='complete' AND memory_id IS NOT NULL").fetchall()
            for row in rows:
                connection.execute('INSERT INTO embedding_rebuild_steps(id,job_id,action_id,memory_id,version,fact_hash) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(uuid5(job['id'],str(row['id'])),job['id'],row['id'],row['memory_id'],row['version'],hashlib.sha256(row['fact'].encode()).hexdigest()))
            connection.execute('INSERT INTO embedding_rebuild_steps(id,job_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(uuid5(job['id'],'probe'),job['id']))
            connection.execute("UPDATE embedding_rebuilds SET initialized=TRUE,phase='verifying' WHERE id=%s",(job['id'],))

    def execute(self, identifier: UUID) -> None:
        """按任务锁执行探针、逐项嵌入和发布，预算或错误只停止未完成部分。"""
        with self.database.connect() as lock:
            lock.autocommit=True
            lock.execute('SELECT pg_advisory_lock(%s)',(identifier.int%(2**63-1),))
            job=self.get(identifier)
            if job['status'] in ('complete','cancelled'):
                return
            try:
                if job['status']=='cancelling' or job['phase']=='cancelling':
                    self.finish_cancel(job)
                    return
                self.change(identifier,'running')
                settings=self.target_settings(job)
                if not job['initialized']:
                    self.initialize(job)
                if job['phase']!='cleanup':
                    with self.database.connect() as connection:
                        steps=connection.execute('SELECT id FROM embedding_rebuild_steps WHERE job_id=%s ORDER BY action_id NULLS FIRST,id',(identifier,)).fetchall()
                    for step in steps:
                        self.step(job,settings,step['id'])
                    self.publish(job,settings)
                self.cleanup(job)
                self.private(identifier).path.unlink(missing_ok=True)
                self.change(identifier,'complete','complete')
            except BudgetPaused as paused:
                self.change(identifier,'paused',pause=paused.public())
            except Exception as error:
                if self.get(identifier)['status']=='cancelling':
                    try:
                        self.finish_cancel(job)
                    except Exception:
                        self.change(identifier,'cancelling',error='目标清理尚未完成，请继续取消。')
                    return
                detail=str(error) if isinstance(error,(ValueError,ModelFailure)) else '重建尚未完成；原记忆正文保留，请重试核对已有步骤。'
                self.change(identifier,'failed',error=detail)

    def step(self, job: dict[str, Any], settings: StoredEmbeddingSettings, identifier: UUID) -> None:
        """每个有效批准版本恰好一个目标写入；恢复先核对目标，再决定是否重新计费。"""
        with self.spaces.lease(maintenance=True), self.database.connect() as lock:
            lock.autocommit=True
            if self.get(job['id'])['status']=='cancelling':
                raise ValueError('任务已取消，未开始新的嵌入请求。')
            step=lock.execute('SELECT * FROM embedding_rebuild_steps WHERE id=%s',(identifier,)).fetchone()
            assert step is not None
            if step['action_id']:
                lock.execute('SELECT pg_advisory_lock(%s)',(step['action_id'].int%(2**63-1),))
                entry=lock.execute("SELECT * FROM memory_actions WHERE id=%s AND status='complete' AND version=%s AND memory_id=%s",(step['action_id'],step['version'],step['memory_id'])).fetchone()
                if entry is None:
                    lock.execute("UPDATE embedding_rebuild_steps SET status='invalidated' WHERE id=%s",(identifier,))
                    return
                text=entry['fact']
                self.change(job['id'],'running','embedding')
            else:
                entry=None
                text='连接验证'
            if step['status']=='invalidated':
                return
            target=self.space(job['target_id'])
            with self.memory.session(settings,collection=target['collection_name']) as sdk:
                current=sdk.vector_store.get(str(step['memory_id'])) if entry else None
                if current is not None and current.payload.get('rebuild_job')==str(job['id']) and current.payload.get('memory_version')==step['version'] and current.payload.get('data')==text and current.payload.get('user_id')=='local-person' and current.payload.get('action_id')==str(step['action_id']):
                    lock.execute("UPDATE embedding_rebuild_steps SET status='complete' WHERE id=%s",(identifier,))
                    return
                if not entry and step['status']=='complete':
                    return
                operation_id=step['operation_id']
                if operation_id:
                    previous=lock.execute('SELECT pause_id,cancelled FROM cloud_operations WHERE id=%s',(operation_id,)).fetchone()
                    has_calls=lock.execute("SELECT id FROM model_calls WHERE operation_id=%s AND status<>'cancelled'",(operation_id,)).fetchone()
                    if previous is None or previous['cancelled'] or (has_calls and not previous['pause_id']):
                        operation_id=None
                operation_id=operation_id or uuid4()
                with self.budget.operation('embedding_rebuild',[str(job['id']),str(identifier),job['target_revision'],step['version']],operation_id) as (operation,_):
                    with lock.transaction():
                        current_job=lock.execute('SELECT status FROM embedding_rebuilds WHERE id=%s FOR SHARE',(job['id'],)).fetchone()
                        if current_job is None or current_job['status']!='running':
                            raise ValueError('重建已停止，未授权新的嵌入步骤。')
                        lock.execute('UPDATE embedding_rebuild_steps SET operation_id=%s WHERE id=%s',(operation_id,identifier))
                        lock.execute('INSERT INTO embedding_rebuild_operations(step_id,operation_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(identifier,operation_id))
                    call_id=self.budget.reserve(operation,settings,'embedding_tokens')
                    lock.execute("UPDATE embedding_rebuild_steps SET status='running',call_id=%s WHERE id=%s",(call_id,identifier))
                    response=self.memory.calls.bind(operation).embed(settings,[text],call_id)
                    with lock.transaction():
                        valid=lock.execute("SELECT * FROM memory_actions WHERE id=%s AND status='complete' AND version=%s FOR SHARE",(step['action_id'],step['version'])).fetchone() if entry else None
                        current_job=lock.execute('SELECT status FROM embedding_rebuilds WHERE id=%s FOR SHARE',(job['id'],)).fetchone()
                        active=lock.execute('SELECT cancelled FROM cloud_operations WHERE id=%s FOR SHARE',(operation_id,)).fetchone()
                        if current_job is None or current_job['status']=='cancelling' or active is None or active['cancelled'] or (entry and valid is None):
                            raise ValueError('重建步骤对应事实或操作已失效，未写入迟到向量。')
                        if valid:
                            sdk.write_rebuilt(str(step['memory_id']),list(response.data[0].embedding),self.payload(job,valid))
                        else:
                            lock.execute("INSERT INTO capability_checks(capability,revision,model,status,detail,dimensions) VALUES('embedding',%s,%s,'passed','重建目标嵌入接口已验证。',%s) ON CONFLICT(capability,revision) DO UPDATE SET status='passed',dimensions=EXCLUDED.dimensions,checked_at=CURRENT_TIMESTAMP",(settings.revision,settings.model,settings.dimensions))
                            lock.execute('UPDATE embedding_rebuilds SET verified=TRUE WHERE id=%s',(job['id'],))
                        lock.execute("UPDATE embedding_rebuild_steps SET status='complete' WHERE id=%s",(identifier,))

    def payload(self, job: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        """沿用真实 SDK 时间及更正元信息，正文只取当前批准值并移除失效来源链接。"""
        old=self.space(job['old_id'])
        settings=StoredEmbeddingSettings(base_url=old['base_url'],model=old['model'],dimensions=old['dimensions'],api_key='')
        with self.memory.session(settings,collection=old['collection_name']) as sdk:
            existing=sdk.vector_store.get(str(entry['memory_id']))
            if existing is not None and (existing.payload.get('user_id')!='local-person' or existing.payload.get('action_id')!=str(entry['id'])):
                raise ValueError('原索引身份不符，未覆盖其他记忆。')
            payload=dict(existing.payload) if existing else {'created_at':entry['created_at'].isoformat(),'updated_at':entry['updated_at'].isoformat()}
        payload.update(data=entry['fact'],hash=hashlib.md5(entry['fact'].encode()).hexdigest(),user_id='local-person',action_id=str(entry['id']),rebuild_job=str(job['id']),memory_version=entry['version'],source_deleted=entry['source_deleted'])
        if entry['source_deleted']:
            payload.pop('source_message_id',None)
            payload.pop('source_conversation_id',None)
        else:
            payload.update(source_message_id=str(entry['message_id']),source_conversation_id=str(entry['conversation_id']))
        return payload

    def publish(self, job: dict[str, Any], settings: StoredEmbeddingSettings) -> None:
        """独占本地发布核对全部有效事实；持久 publishing 阶段保护文件与 DB 间隙。"""
        with self.spaces.lease(exclusive=True,maintenance=True):
            with self.database.connect() as connection:
                current=connection.execute('SELECT status FROM embedding_rebuilds WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
                if current is None or current['status']=='cancelling':
                    raise ValueError('重建已取消，未发布目标。')
                valid=connection.execute("SELECT * FROM memory_actions WHERE status='complete' AND memory_id IS NOT NULL FOR SHARE").fetchall()
                target=self.space(job['target_id'])
                for entry in valid:
                    with self.memory.session(settings,collection=target['collection_name']) as sdk:
                        stored=sdk.vector_store.get(str(entry['memory_id']))
                        if stored is None or stored.payload.get('user_id')!='local-person' or stored.payload.get('action_id')!=str(entry['id']) or stored.payload.get('memory_version')!=entry['version'] or stored.payload.get('data')!=entry['fact']:
                            raise ValueError('目标索引尚未完整或身份不符，未切换配置；请继续修复缺失步骤。')
                        # 来源保留可能在该步骤完成后变化；发布前按真实关系清旧链接。
                        payload=dict(stored.payload)
                        payload['source_deleted']=entry['source_deleted']
                        if entry['source_deleted']:
                            payload.pop('source_message_id',None)
                            payload.pop('source_conversation_id',None)
                        if payload!=stored.payload:
                            sdk.vector_store.update(str(entry['memory_id']),payload=payload)
                    complete=connection.execute("SELECT id FROM embedding_rebuild_steps WHERE job_id=%s AND action_id=%s AND memory_id=%s AND version=%s AND status='complete'",(job['id'],entry['id'],entry['memory_id'],entry['version'])).fetchone()
                    if complete is None:
                        raise ValueError('有效记忆发生变化，目标尚未完整覆盖，未切换配置。')
                connection.execute("UPDATE embedding_rebuilds SET phase='publishing' WHERE id=%s",(job['id'],))
            self.embedding.store.write(settings)
            with self.database.connect() as connection:
                connection.execute("UPDATE embedding_spaces SET status='retired' WHERE status='active' AND id<>%s",(job['target_id'],))
                connection.execute("UPDATE embedding_spaces SET status='active' WHERE id=%s",(job['target_id'],))
                connection.execute("UPDATE embedding_indexes SET base_url=%s,model=%s,dimensions=%s WHERE name='personal_memories'",(settings.base_url,settings.model,settings.dimensions))
                connection.execute("UPDATE embedding_rebuilds SET phase='cleanup' WHERE id=%s",(job['id'],))

    def cleanup(self, job: dict[str, Any]) -> None:
        """待删除对账完成后清退休集合；未完成删除或本地失败保留新查询及旧空间清理入口。"""
        with self.spaces.lease(exclusive=True,maintenance=True), self.database.connect() as connection:
            if connection.execute("SELECT id FROM memory_actions WHERE status='deleting' LIMIT 1").fetchone():
                raise ValueError('新索引已可用，仍有记忆删除未完成；请先重试记忆清理，再继续旧索引清理。')
            spaces=connection.execute("SELECT * FROM embedding_spaces WHERE status='retired'").fetchall()
            for space in spaces:
                connection.execute(sql.SQL('DROP TABLE IF EXISTS {}').format(sql.Identifier(space['collection_name'])))
                connection.execute("UPDATE embedding_spaces SET status='removed' WHERE id=%s",(space['id'],))
