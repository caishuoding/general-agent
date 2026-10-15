"""将当前用户授权与回答材料分开，供同步和流式入口共用。"""

import re
from dataclasses import dataclass
from uuid import UUID
from typing import Any

from general_agent.budget import OperationContext
from general_agent.calls import CallGateway
from general_agent.memory import MemoryService
from general_agent.memory_deletion import MemoryDeletion, deletion_intent
from general_agent.attachment_memory import AttachmentMemory, attachment_memory_intent
from general_agent.memory_candidates import MemoryCandidates, candidate_eligible
from general_agent.memory_correction import MemoryCorrection, correction_intent
from general_agent.recall import MemoryRecall
from general_agent.provider import ModelMessage
from general_agent.settings import StoredSettings
from general_agent.summary import DocumentSummary, SummaryPreparation, summary_mode
from general_agent.attachments import AttachmentContext
from general_agent.history import HistoryContext, HistorySearch, history_query
from general_agent.search import SearchService, search_query


@dataclass(frozen=True)
class Intent:
    """仅从当前输入识别的明确事实；不从历史、模型或资料中获取授权。"""
    fact: str | None = None
    clarification: str | None = None
    answer: bool = False


def explicit_intent(content: str) -> Intent:
    """识别独立句首的明确记忆指令，歧义对象或资料引用返回澄清而不写入。"""
    matches = []
    remainder = []
    for part in re.split(r'[；;\n]', content):
        match = re.fullmatch(r'\s*(?:请)?(?:帮我)?(?:记住|记下来|记一下)\s*[：:]?\s*(.*?)\s*', part)
        if match:
            matches.append(match.group(1).strip())
        elif part.strip() and not re.fullmatch(r"\s*附件供参考\s*", part):
            remainder.append(part)
    if not matches:
        return Intent()
    fact = matches[0]
    if re.search(r'(写|创作|编|虚构).{0,12}(故事|小说|角色|台词)|(?:故事|小说|虚构)(?:里|中|角色)|假设[：:]?', content):
        return Intent(clarification='这条消息包含写作或假设情境，未保存为个人长期记忆。若要保存真实信息，请单独明确具体事实。')
    if len(matches) != 1 or not fact or len(fact) > 5000 or re.search(r'[？?]', fact) or re.match(r'(这个|这些|这件事|上面|刚才|之前|附件|文件|文档|以下|如下|它|他的话)', fact) or re.search(r'(功能|这句话的意思|这个词)', fact):
        return Intent(clarification='请直接写出要长期保存的具体事实，例如“请记住：我喜欢简洁的中文回答”。引用或附件内容需要先核对具体事实。')
    return Intent(fact=fact.rstrip('。'), answer=bool(remainder))


@dataclass(frozen=True)
class AnswerPreparation:
    """统一回答材料和本地可复用结果，传输入口只负责发送与持久化各自的答案。"""
    messages: list[ModelMessage]
    material: AttachmentContext
    history: HistoryContext | None
    summary: SummaryPreparation | None
    reserved: UUID | None
    local_answer: str | None


class ChatExecution:
    """共用批准动作和回答准备；传输、线程及答案尝试留给聊天入口。"""

    def __init__(self, calls: CallGateway, memory: MemoryService, search: SearchService) -> None:
        """绑定共同动作与唯一材料路由，全文检查点和查询优先级由同步及流式入口共享。"""
        self.calls, self.memory = calls, memory
        self.recall = MemoryRecall(memory)
        self.search = search
        self.history = HistorySearch(memory.database)
        self.correction = MemoryCorrection(memory)
        self.candidates = MemoryCandidates(memory)
        self.deletion = MemoryDeletion(memory)
        self.attachment_memory = AttachmentMemory(self.candidates, search.attachments)
        self.documents = DocumentSummary(memory.database,calls,search.attachments)

    def embedding_revision(self, content: str) -> str | None:
        """本地删除与只读历史不绑定嵌入，其余记忆动作按配置版本约束暂停恢复。"""
        if history_query(content) is not None or deletion_intent(content) is not None:
            return None
        embedding = self.memory.embedding.store.read() if explicit_intent(content).fact or correction_intent(content) is not None or self.recall.available() else None
        return embedding.revision if embedding else None

    def same_embedding_revision(self, expected: str | None) -> bool:
        """续跑只核验原来涉及的嵌入配置，不把记忆数量变化误当配置修改。"""
        current=self.memory.embedding.store.read()
        return expected is None or (current is not None and current.revision==expected)

    def scope(self, content: str, settings: StoredSettings) -> list[str | None]:
        """将嵌入及明确搜索的配置版本纳入幂等范围，批准不能转用于新服务。"""
        search = self.search.settings.read() if search_query(content) else None
        return [content, settings.revision, self.embedding_revision(content), search.revision if search else None]

    def reserve_first(self, content: str, settings: StoredSettings, operation: OperationContext, user_id: UUID | None = None) -> UUID | None:
        """本地删除不申请预算，历史与附件候选先登记状态，其余预留真实首步。"""
        if history_query(content) is not None or deletion_intent(content) is not None or attachment_memory_intent(content):
            return None
        if correction_intent(content) is not None:
            # 更正先持久化目标版本并停用旧值，再在步骤内申请预算。
            return None
        intent = explicit_intent(content)
        if intent.clarification:
            return None
        if intent.fact:
            existing = self.memory.action(user_id) if user_id else None
            if existing:
                # 重试先核对已落向量；只有确实需要嵌入时才在工作步骤预留。
                return None
            # 缺失配置由已保存的动作展示为可重试失败，普通聊天不受影响。
            try:
                embedding = self.memory.embedding.verified()
            except ValueError:
                return None
            return self.calls.budget.reserve(operation, embedding, 'embedding_tokens')
        if candidate_eligible(content) or search_query(content):
            return None
        if self.recall.available():
            try:
                embedding = self.memory.embedding.verified()
            except ValueError:
                return None
            return self.calls.budget.reserve(operation, embedding, 'embedding_tokens')
        return None if summary_mode(content) else self.calls.budget.reserve(operation, settings)

    def prepare_answer(self, user_id: UUID, content: str, operation: OperationContext, reserved: UUID | None) -> list[ModelMessage]:
        """提供正式相关记忆及候选授权提示；当前个人表达不得被回答模型声称为自动保存。"""
        if explicit_intent(content).fact:
            return []
        with self.memory.database.connect() as connection:
            row=connection.execute('SELECT unit,status FROM model_calls WHERE id=%s',(reserved,)).fetchone() if reserved else None
        material=self.recall.prepare(user_id,content,operation,reserved,skip=bool(row and row['unit']=='chat_tokens'))
        if candidate_eligible(content):
            material.append({'role':'system','content':'当前用户消息的自动记忆识别仅能提出候选，不代表已保存或更正。不要声称自动记住了这条信息；是否写入长期记忆以应用确认卡片的真实状态为准，待确认候选需要用户点击批准。'})
        return material

    def prepare_response(self, conversation_id: UUID, user_id: UUID, message_id: UUID, settings: StoredSettings, operation: OperationContext, reserved: UUID | None, rows: list[dict[str, Any]]) -> AnswerPreparation:
        """统一历史→联网→附件/记忆/全文优先级，复用检查点和真实首步预留，不执行传输。"""
        content=str(rows[-1]['content'])
        history=self.history.prepare(conversation_id,user_id,content,operation)
        external=history.messages if history else self.search.prepare(conversation_id,user_id,content,settings,operation)
        chat_reserved=self.chat_reservation(content,reserved)
        material=AttachmentContext()
        summary=None
        if external is None:
            if summary_mode(content) is None:
                material=self.search.attachments.prepare(conversation_id,user_id,content,settings=settings)
            memory_material=self.prepare_answer(user_id,content,operation,reserved)
            summary=self.documents.prepare(conversation_id,user_id,message_id,content,settings,operation,chat_reserved,memory_material)
            if summary:
                material=summary.material
        else:
            memory_material=external
        conversation: list[ModelMessage]=[{'role':str(row['role']),'content':str(row['content'])} for row in (rows[-1:] if summary or history else rows)]
        local_answer=history.local_answer if history else summary.cached_answer if summary else None
        return AnswerPreparation(memory_material+material.messages+conversation,material,history,summary,chat_reserved,local_answer)

    def chat_reservation(self, content: str, reserved: UUID | None) -> UUID | None:
        """仅复用尚未启动的聊天预留；候选、保存或召回已经用过的请求不得再次启动。"""
        if reserved is None:
            return None
        with self.memory.database.connect() as connection:
            row=connection.execute('SELECT unit,status FROM model_calls WHERE id=%s',(reserved,)).fetchone()
        return reserved if row and row['unit']=='chat_tokens' and row['status']=='reserved' else None

    def prepare(self, conversation_id: UUID, user_id: UUID, content: str, operation: OperationContext, reserved: UUID | None, settings: StoredSettings | None = None) -> str | None:
        """历史保持只读；本地删除优先，附件明确提取先预览，其余保存、更正及个人事实沿授权执行。"""
        if history_query(content) is not None:
            return None
        if deletion_intent(content) is not None:
            return self.deletion.chat(content,user_id)
        if attachment_memory_intent(content) and settings is not None:
            return self.attachment_memory.prepare(conversation_id,user_id,settings,operation)
        if correction_intent(content) is not None:
            return self.correction.chat(content,user_id,conversation_id,operation)
        intent = explicit_intent(content)
        if intent.clarification:
            return intent.clarification
        if intent.fact:
            saved = self.memory.save(conversation_id, user_id, intent.fact, operation, reserved)
            return None if intent.answer else '已保存长期记忆：' + str(saved['fact'])
        if settings is not None:
            self.candidates.discover(conversation_id,user_id,settings,operation)
        return None
