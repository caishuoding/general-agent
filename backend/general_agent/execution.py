"""将当前用户授权与回答材料分开，供同步和流式入口共用。"""

import re
from dataclasses import dataclass
from uuid import UUID

from general_agent.budget import OperationContext
from general_agent.calls import CallGateway
from general_agent.memory import MemoryService
from general_agent.memory_correction import MemoryCorrection, correction_intent
from general_agent.recall import MemoryRecall
from general_agent.provider import ModelMessage
from general_agent.settings import StoredSettings
from general_agent.summary import summary_mode
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


class ChatExecution:
    """共用批准动作和回答准备；传输、线程及答案尝试留给聊天入口。"""

    def __init__(self, calls: CallGateway, memory: MemoryService, search: SearchService) -> None:
        """绑定本地动作、明确搜索及唯一云端调用边界。"""
        self.calls, self.memory = calls, memory
        self.recall = MemoryRecall(memory)
        self.search = search
        self.correction = MemoryCorrection(memory)

    def embedding_revision(self, content: str) -> str | None:
        """保存、更正或召回涉及嵌入时绑定版本，暂停尝试不能沿用修改前的配置批准。"""
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
        """更正先停用旧值，搜索和全文在具体步骤申请预算，其余只预留真实下一次调用。"""
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
        if search_query(content):
            return None
        if self.recall.available():
            try:
                embedding = self.memory.embedding.verified()
            except ValueError:
                return None
            return self.calls.budget.reserve(operation, embedding, 'embedding_tokens')
        return None if summary_mode(content) else self.calls.budget.reserve(operation, settings)

    def prepare_answer(self, user_id: UUID, content: str, operation: OperationContext, reserved: UUID | None) -> list[ModelMessage]:
        """普通回答提供已批准相关记忆，兼容文字或视觉消息组合；明确保存维持独立回执。"""
        if explicit_intent(content).fact:
            return []
        with self.memory.database.connect() as connection:
            row=connection.execute('SELECT unit FROM model_calls WHERE id=%s',(reserved,)).fetchone() if reserved else None
        return self.recall.prepare(user_id,content,operation,reserved,skip=bool(row and row['unit']=='chat_tokens'))

    def chat_reservation(self, content: str, reserved: UUID | None) -> UUID | None:
        """仅复用聊天预留；记忆保存或召回用过的嵌入请求不得挪作回答调用。"""
        if reserved is None:
            return None
        with self.memory.database.connect() as connection:
            row=connection.execute('SELECT unit FROM model_calls WHERE id=%s',(reserved,)).fetchone()
        return reserved if row and row['unit']=='chat_tokens' else None

    def prepare(self, conversation_id: UUID, user_id: UUID, content: str, operation: OperationContext, reserved: UUID | None) -> str | None:
        """执行当前明确保存或更正并返回真实回执；空值表示继续普通回答。"""
        if correction_intent(content) is not None:
            return self.correction.chat(content,user_id,conversation_id,operation)
        intent = explicit_intent(content)
        if intent.clarification:
            return intent.clarification
        if intent.fact:
            saved = self.memory.save(conversation_id, user_id, intent.fact, operation, reserved)
            return None if intent.answer else '已保存长期记忆：' + str(saved['fact'])
        return None
