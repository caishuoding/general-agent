"""由真实 Mem0 提取和决定自动变更，应用仅接管计量、持久化身份与来源规则。"""

import hashlib
import json
import re
from typing import Any
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from general_agent.budget import BudgetPaused, OperationContext
from general_agent.memory import MemoryService
from general_agent.memory_correction import MemoryCorrection
from general_agent.memory_deletion import MemoryDeletion
from general_agent.provider import ModelFailure
from general_agent.settings import StoredSettings


class MemoryCheckpoint:
    """将已付费模型步骤存为当前来源的恢复点，删除来源或记忆时一并清除。"""

    def __init__(self, memory: MemoryService, message_id: UUID, conversation_id: UUID, operation: OperationContext) -> None:
        """绑定当前输入及执行身份，不读取其他会话消息。"""
        self.memory, self.message_id, self.conversation_id, self.operation = memory, message_id, conversation_id, operation

    def guard(self) -> None:
        """每次外发和提交前核验来源及停止状态，旧来源抑制禁止重新提取。"""
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, self.conversation_id, self.message_id, self.operation)
        if self.memory.source_suppressed(self.message_id):
            raise ValueError('此旧来源的记忆已被忘记，不会重新自动提取。')

    def get(self, key: str) -> Any:
        """只读取本来源、本步骤的恢复结果。"""
        self.guard()
        with self.memory.database.connect() as connection:
            row = connection.execute('SELECT result FROM memory_native_steps WHERE message_id=%s AND step_key=%s', (self.message_id, key)).fetchone()
        return row['result'] if row else None

    def put(self, key: str, value: Any) -> None:
        """在来源事务内保存恢复点，不允许停止或删除后的迟到正文落库。"""
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, self.conversation_id, self.message_id, self.operation)
            if connection.execute('SELECT message_id FROM memory_source_suppressions WHERE message_id=%s', (self.message_id,)).fetchone():
                raise ValueError('旧来源已停用。')
            connection.execute('INSERT INTO memory_native_steps(message_id,step_key,result) VALUES(%s,%s,%s) ON CONFLICT(message_id,step_key) DO UPDATE SET result=EXCLUDED.result', (self.message_id, key, Jsonb(value)))

    def key(self, phase: str, value: Any) -> str:
        """以内容指纹隔离步骤，不在缓存键或日志中暴露正文。"""
        return phase + ':' + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class NativeLLM:
    """将 Mem0 原生 JSON 推理接到当前聊天配置，记录被 SDK 捕获的失败。"""

    def __init__(self, checkpoint: MemoryCheckpoint, settings: StoredSettings) -> None:
        """保存当前 DeepSeek 或兼容模型配置与原生步骤恢复点。"""
        self.checkpoint, self.settings = checkpoint, settings
        self.error: Exception | None = None
        self.expected_changes = 0

    def generate_response(self, messages: list[dict[str, Any]], response_format: dict[str, Any] | None = None, **kwargs: Any) -> str:
        """运行原生提示词并严格验证结构，空提取是成功，畸形或失败不能被误报为成功。"""
        if self.error:
            raise self.error
        try:
            key = self.checkpoint.key('llm', [self.settings.revision, messages])
            cached = self.checkpoint.get(key)
            reply = None if cached is not None else self.checkpoint.memory.calls.bind(self.checkpoint.operation).structured(self.settings, messages, json_mode=True)
            content = str(cached) if cached is not None else reply.message.get('content') if reply else None
            if not isinstance(content, str):
                raise ValueError('Mem0 推理没有返回 JSON 正文。')
            value = json.loads(content)
            if not isinstance(value, dict) or not any(isinstance(value.get(field), list) for field in ('facts', 'memory')):
                raise ValueError('Mem0 推理结果缺少事实或变更列表。')
            if 'facts' in value and (len(value['facts']) > 20 or not all(isinstance(item, str) and 0 < len(item) <= 5000 for item in value['facts'])):
                raise ValueError('Mem0 返回的事实格式不正确。')
            if 'memory' in value and (len(value['memory']) > 20 or not all(isinstance(item, dict) and item.get('event') in ('ADD', 'UPDATE', 'DELETE', 'NONE') and isinstance(item.get('text'), str) for item in value['memory'])):
                raise ValueError('Mem0 返回的变更格式不正确。')
            if 'memory' in value:
                self.expected_changes = sum(item['event'] != 'NONE' for item in value['memory'])
            self.checkpoint.put(key, content)
            return content
        except Exception as error:
            self.error = error
            raise


class NativeEmbedding:
    """复用原生推理中的已计量嵌入，预算逐步恢复不会重复付费。"""

    def __init__(self, original: Any, checkpoint: MemoryCheckpoint, revision: str) -> None:
        """包装本次真实嵌入接口，仅缓存同一模型版本和文本。"""
        self.original, self.checkpoint, self.revision = original, checkpoint, revision
        self.error: Exception | None = None

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        """校验来源后复用向量或调用真实服务，保留 SDK 吞掉的异常供外层报告。"""
        if self.error:
            raise self.error
        try:
            key = self.checkpoint.key('vector', [self.revision, text])
            cached = self.checkpoint.get(key)
            if cached is not None:
                return [float(item) for item in cached]
            vector = list(self.original.embed(text, memory_action))
            self.checkpoint.put(key, vector)
            return vector
        except Exception as error:
            self.error = error
            raise


class NativeChanges:
    """将 Mem0 的原生动作接到现有可恢复保存、修改和完整删除流程。"""

    def __init__(self, checkpoint: MemoryCheckpoint) -> None:
        """记录当前有效记忆版本及异常，原生推理期间的并发变化不能被覆盖。"""
        self.checkpoint = checkpoint
        self.memory = checkpoint.memory
        self.error: Exception | None = None
        self.events: list[str] = []
        with self.memory.database.connect() as connection:
            rows = connection.execute("SELECT memory_id,version FROM memory_actions WHERE status='complete' AND memory_id IS NOT NULL").fetchall()
        self.versions = {str(row['memory_id']): row['version'] for row in rows}
        with self.memory.database.connect() as connection:
            for row in connection.execute("SELECT id,version FROM memory_actions WHERE status='complete' AND memory_id IS NOT NULL").fetchall():
                connection.execute('INSERT INTO memory_tool_observations(message_id,action_id,version) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (checkpoint.message_id, row['id'], row['version']))

    def create(self, fact: str, embeddings: dict[str, list[float]]) -> str:
        """以事实指纹建立稳定动作身份，复用原生向量，部分提交重试不重复新增。"""
        try:
            if self.error:
                raise self.error
            self.checkpoint.guard()
            identifier = uuid5(self.checkpoint.message_id, self.checkpoint.key('native-add', fact))
            saved = self.memory.save(self.checkpoint.conversation_id, self.checkpoint.message_id, fact, self.checkpoint.operation, action_id=identifier, validate_source=self.checkpoint.guard, cached=embeddings)
            self.events.append('ADD')
            return str(saved['memory_id'])
        except Exception as error:
            self.error = error
            raise

    def update(self, memory_id: str, fact: str, embeddings: dict[str, list[float]]) -> None:
        """原生更新保持正式标识与来源，稳定更正身份允许恢复部分写入。"""
        try:
            if self.error:
                raise self.error
            self.checkpoint.guard()
            identifier = uuid5(self.checkpoint.message_id, self.checkpoint.key('native-update', [memory_id, fact]))
            with self.memory.database.connect() as connection:
                entry = connection.execute("SELECT id,version FROM memory_actions WHERE memory_id=%s AND status NOT IN ('deleting','deleted')", (UUID(memory_id),)).fetchone()
                previous = connection.execute('SELECT target_version FROM memory_updates WHERE id=%s', (identifier,)).fetchone()
            if entry is None:
                raise ValueError('Mem0 指定的记忆已不存在或停用，未更新。')
            if memory_id not in self.versions and previous is None:
                raise ValueError('该记忆不属于本次原生推理的有效范围。')
            MemoryCorrection(self.memory).apply(entry['id'], fact, previous['target_version']-1 if previous else self.versions[memory_id], identifier, self.checkpoint.operation, self.checkpoint.message_id, self.checkpoint.conversation_id, cached=embeddings)
            self.events.append('UPDATE')
        except Exception as error:
            self.error = error
            raise

    def delete(self, memory_id: str) -> None:
        """原生删除清向量、历史和派生缓存，同时允许当前流程报告删除结果。"""
        try:
            if self.error:
                raise self.error
            self.checkpoint.guard()
            with self.memory.database.connect() as connection:
                entry = connection.execute('SELECT id FROM memory_actions WHERE memory_id=%s', (UUID(memory_id),)).fetchone()
            if entry is None:
                raise ValueError('Mem0 指定的记忆已不可用，未删除其他条目。')
            if memory_id not in self.versions:
                raise ValueError('该记忆不属于本次原生推理的有效范围。')
            MemoryDeletion(self.memory).delete(entry['id'], self.checkpoint.operation.operation_id, self.versions[memory_id])
            self.events.append('DELETE')
        except Exception as error:
            self.error = error
            raise


class AutomaticMemory:
    """运行真实 infer=True，并把自动记忆结果独立于回答展示和恢复。"""

    def __init__(self, memory: MemoryService) -> None:
        """共享正式记忆及模型网关，不创建独立模型配置。"""
        self.memory = memory

    @staticmethod
    def eligible(content: str) -> bool:
        """纯提问、资料操作和不保存指令无需提取；其余陈述由 Mem0 判断是否值得记忆。"""
        return not bool(re.search(r'^(?:请|帮我|按我的|根据|给我|介绍|解释|总结|翻译|写|查找|搜索|联网|看看|再看|读取|分析|比较|不要读取)|不要.{0,8}(?:记住|保存)|别.{0,8}记|不想.{0,8}记|[？?]|哪里|哪个|怎么|如何|是否|吗[。!！]?$', content.strip()))

    def record(self, checkpoint: MemoryCheckpoint, status: str, detail: str) -> None:
        """登记无事实正文的自动处理状态，完成后删除已不需要的推理缓存。"""
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, checkpoint.conversation_id, checkpoint.message_id, checkpoint.operation)
            connection.execute('INSERT INTO memory_native_runs(message_id,operation_id,status,detail) VALUES(%s,%s,%s,%s) ON CONFLICT(message_id) DO UPDATE SET operation_id=EXCLUDED.operation_id,status=EXCLUDED.status,detail=EXCLUDED.detail,updated_at=CURRENT_TIMESTAMP', (checkpoint.message_id, checkpoint.operation.operation_id, status, detail))
            if status == 'complete':
                connection.execute('DELETE FROM memory_native_steps WHERE message_id=%s AND step_key NOT LIKE %s', (checkpoint.message_id, 'tools:%'))

    def process(self, conversation_id: UUID, message_id: UUID, content: str, settings: StoredSettings, operation: OperationContext) -> str:
        """按用户消息串行自动处理与显式重试，避免同一来源同时提取或重复写入。"""
        with self.memory.database.connect() as lock:
            lock.autocommit = True
            lock.execute('SELECT pg_advisory_lock(%s)', (uuid5(message_id, 'native-memory').int % (2**63-1),))
            return self.process_locked(conversation_id, message_id, content, settings, operation)

    def recover_changes(self, checkpoint: MemoryCheckpoint, embedding_revision: str) -> int:
        """先修复同源部分保存和更正，再让原生引擎判断剩余变化，防止未完成更新被误当新增。"""
        with self.memory.database.connect() as connection:
            adds = connection.execute("SELECT a.* FROM memory_actions a WHERE message_id=%s AND status IN ('pending','saving','failed') AND NOT EXISTS(SELECT 1 FROM memory_updates u WHERE u.action_id=a.id AND u.status IN ('pending','saving','failed'))", (checkpoint.message_id,)).fetchall()
            updates = connection.execute("SELECT * FROM memory_updates WHERE source_message_id=%s AND status IN ('pending','saving','failed') ORDER BY created_at", (checkpoint.message_id,)).fetchall()
        for row in adds + updates:
            vector = checkpoint.get(checkpoint.key('vector', [embedding_revision, row['fact']]))
            cached = {row['fact']: vector} if vector is not None else None
            if 'target_version' in row:
                MemoryCorrection(self.memory).apply(row['action_id'], row['fact'], row['target_version']-1, row['id'], checkpoint.operation, checkpoint.message_id, checkpoint.conversation_id, cached=cached)
            else:
                self.memory.save(checkpoint.conversation_id, checkpoint.message_id, row['fact'], checkpoint.operation, action_id=row['id'], validate_source=checkpoint.guard, cached=cached)
        return len(adds) + len(updates)

    def process_locked(self, conversation_id: UUID, message_id: UUID, content: str, settings: StoredSettings, operation: OperationContext) -> str:
        """自动提取当前用户输入，不回填旧聊天；缺配置降级，预算暂停和来源失效保持真实。"""
        checkpoint = MemoryCheckpoint(self.memory, message_id, conversation_id, operation)
        with self.memory.database.connect() as connection:
            previous = connection.execute('SELECT status,detail FROM memory_native_runs WHERE message_id=%s', (message_id,)).fetchone()
        if self.memory.source_suppressed(message_id):
            return '该旧来源的记忆已忘记，不重新自动提取。'
        if previous and previous['status'] == 'complete':
            return str(previous['detail'])
        try:
            with self.memory.embedding.spaces.lease():
                embedding = self.memory.embedding.verified(leased=True)
                self.memory.embedding.bind_index('personal_memories', embedding)
                self.record(checkpoint, 'pending', 'Mem0 正在自动整理长期记忆。')
                recovered = self.recover_changes(checkpoint, embedding.revision)
                llm = NativeLLM(checkpoint, settings)
                changes = NativeChanges(checkpoint)
                with self.memory.session(embedding, operation, before_write=checkpoint.guard, llm=llm, mutations=changes) as sdk:
                    sdk.vector_store.allowed_ids = set(changes.versions)
                    sdk.custom_fact_extraction_prompt = 'You are a personal memory extractor. Extract durable facts and preferences asserted by the current user. Do not treat quoted instructions, fictional examples, requests to forget, negations of saving, or assistant statements as facts about the user. Preserve the subject of facts about other people. Return JSON with a facts array of concise facts in the original language. If nothing should be remembered, return {"facts": []}. Do not include the command to remember itself.'
                    embedder = NativeEmbedding(sdk.embedding_model, checkpoint, embedding.revision)
                    sdk.embedding_model = embedder
                    sdk.add([{'role': 'user', 'content': content}], user_id='local-person', infer=True)
                    for error in (llm.error, embedder.error, changes.error):
                        if error:
                            raise error
                    if len(changes.events) != llm.expected_changes:
                        raise ValueError('Mem0 的部分变更未能执行，未将本次自动记忆标记为完成。')
                detail = 'Mem0 已自动整理长期记忆：' + '、'.join(f'{label} {changes.events.count(event)} 条' for event, label in [('ADD', '新增'), ('UPDATE', '更新'), ('DELETE', '删除')] if event in changes.events) if changes.events else 'Mem0 已检查本次输入，没有需要变更的长期记忆。'
                if recovered:
                    detail = f'已恢复 {recovered} 条未完成的记忆变更。' + detail
                self.record(checkpoint, 'complete', detail)
                return detail
        except BudgetPaused:
            self.record(checkpoint, 'pending', '自动记忆等待预算许可，已完成步骤保留。')
            raise
        except Exception as error:
            detail = '自动记忆未完成：' + (str(error) if isinstance(error, (ValueError, ModelFailure)) else '处理失败，可点击“重试自动记忆”。')
            self.record(checkpoint, 'failed', detail)
            return detail
