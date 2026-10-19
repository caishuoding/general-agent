"""调用官方 Mem0 infer API 处理当前用户消息的自动长期记忆。"""

import hashlib
import json
from typing import Any
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelFailure
from general_agent.infrastructure.settings import StoredSettings
from general_agent.memory.mem0_adapter import Mem0ApiError
from general_agent.memory.service import MemoryService


class MemoryCheckpoint:
    """保存当前来源的可恢复步骤，避免重试重复执行业务动作。"""

    def __init__(self, memory: MemoryService, message_id: UUID, conversation_id: UUID, operation: OperationContext) -> None:
        """绑定消息、会话和操作身份。"""
        self.memory, self.message_id, self.conversation_id, self.operation = memory, message_id, conversation_id, operation

    def guard(self) -> None:
        """确认来源仍存在且未被取消或忘记。"""
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, self.conversation_id, self.message_id, self.operation)
        if self.memory.source_suppressed(self.message_id):
            raise ValueError("此旧来源的记忆已被忘记，不会重新自动提取。")

    def get(self, key: str) -> Any:
        """读取当前来源的已保存步骤。"""
        self.guard()
        with self.memory.database.connect() as connection:
            row = connection.execute("SELECT result FROM memory_native_steps WHERE message_id=%s AND step_key=%s", (self.message_id, key)).fetchone()
        return row["result"] if row else None

    def put(self, key: str, value: Any) -> None:
        """在来源有效期间写入可恢复步骤。"""
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, self.conversation_id, self.message_id, self.operation)
            connection.execute("INSERT INTO memory_native_steps(message_id,step_key,result) VALUES(%s,%s,%s) ON CONFLICT(message_id,step_key) DO UPDATE SET result=EXCLUDED.result", (self.message_id, key, Jsonb(value)))

    def key(self, phase: str, value: Any) -> str:
        """用内容指纹构造不泄露正文的步骤键。"""
        return phase + ":" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class AutomaticMemory:
    """让独立 Mem0 服务负责提取、更新、删除判断和向量写入。"""

    def __init__(self, memory: MemoryService) -> None:
        """绑定应用业务账本和 Mem0 REST 会话工厂。"""
        self.memory = memory

    def record(self, checkpoint: MemoryCheckpoint, status: str, detail: str) -> None:
        """记录自动记忆运行状态，不复制 Mem0 内部历史。"""
        with self.memory.database.connect() as connection:
            self.memory.guard_source(connection, checkpoint.conversation_id, checkpoint.message_id, checkpoint.operation)
            connection.execute("INSERT INTO memory_native_runs(message_id,operation_id,status,detail) VALUES(%s,%s,%s,%s) ON CONFLICT(message_id) DO UPDATE SET operation_id=EXCLUDED.operation_id,status=EXCLUDED.status,detail=EXCLUDED.detail,updated_at=CURRENT_TIMESTAMP", (checkpoint.message_id, checkpoint.operation.operation_id, status, detail))

    def process(self, conversation_id: UUID, message_id: UUID, content: str, settings: StoredSettings, operation: OperationContext, explicit: bool = False) -> str:
        """处理单条当前用户消息。"""
        return self.process_messages(conversation_id, message_id, [{"role": "user", "content": content}], settings, operation, explicit=explicit)

    def process_messages(self, conversation_id: UUID, message_id: UUID, messages: list[dict[str, str]], settings: StoredSettings, operation: OperationContext, track_run: bool = True, explicit: bool = False) -> str:
        """调用 Mem0 add(infer=True)，并把新增结果登记到业务账本。"""
        account_id = self.memory.account_id_for_conversation(conversation_id)
        checkpoint = MemoryCheckpoint(self.memory, message_id, conversation_id, operation)
        explicit_action_id = uuid5(message_id, 'explicit-memory:0')
        if self.memory.source_suppressed(message_id):
            return "该旧来源的记忆已忘记，不重新自动提取。"
        if explicit:
            previous = self.memory.begin_explicit(conversation_id, message_id, operation, explicit_action_id)
            if previous['status'] == 'complete':
                return "该来源的长期记忆已经保存。"
        if track_run:
            self.record(checkpoint, "pending", "Mem0 正在自动整理长期记忆。")
        try:
            metadata = {
                "source_message_id": str(message_id),
                "source_conversation_id": str(conversation_id),
            }
            if explicit:
                metadata["action_id"] = str(explicit_action_id)
            with self.memory.session() as sdk:
                result = sdk.add(messages, user_id=str(account_id), metadata=metadata, infer=True)
                items = sdk._results(result)
            events = [str(item.get("event", "ADD")).upper() for item in items]
            additions = [item for item in items if item.get("id") and item.get("memory") and str(item.get("event", "ADD")).upper() == "ADD"]
            updates = [item for item in items if item.get("id") and item.get("memory") and str(item.get("event", "")).upper() == "UPDATE"]
            deletions = [item for item in items if item.get("id") and str(item.get("event", "")).upper() == "DELETE"]
            for index, item in enumerate(additions):
                if explicit and index == 0:
                    self.memory.complete_explicit(conversation_id, message_id, operation, explicit_action_id, str(item["memory"]), str(item["id"]))
                else:
                    action_id = uuid5(message_id, checkpoint.key("native-result", [item["id"], item["memory"]]))
                    self.memory.register_existing(conversation_id, message_id, str(item["memory"]), str(item["id"]), operation, action_id)
            for item in updates:
                self.memory.apply_native_update(conversation_id, message_id, operation, str(item["id"]), str(item["memory"]), str(item.get("old_memory") or ""))
            for item in deletions:
                with self.memory.database.connect() as connection:
                    target = connection.execute("SELECT id FROM memory_actions WHERE memory_id=%s AND user_id=%s AND status='complete'", (str(item["id"]), account_id)).fetchone()
                if target is not None:
                    from general_agent.memory.deletion import MemoryDeletion
                    MemoryDeletion(self.memory).delete(target['id'], operation.operation_id)
            if explicit and not additions and not updates and not deletions:
                self.memory.fail_explicit(explicit_action_id, 'Mem0 未生成新的长期记忆，可能已存在等价事实。')
            detail = "自动记忆处理完成。" if events else "自动记忆处理完成，无需变更。"
            if track_run:
                self.record(checkpoint, "complete", detail)
            return detail
        except Exception as error:
            if isinstance(error, Mem0ApiError):
                detail = "自动记忆未完成：长期记忆服务暂时不可用，请稍后重试。"
            else:
                detail = "自动记忆未完成：" + (str(error) if isinstance(error, (ValueError, ModelFailure)) else f"处理失败（{type(error).__name__}），可点击“重试自动记忆”。")
            if explicit:
                self.memory.fail_explicit(explicit_action_id, detail)
            if track_run:
                try:
                    self.record(checkpoint, "failed", detail)
                except ValueError:
                    # 用户停止会先取消来源；此时只保留动作失败账本，不再恢复已取消的运行状态。
                    pass
            return detail

