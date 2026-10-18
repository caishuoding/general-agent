"""明确联网请求、受控搜索材料与个人信息外发许可的业务边界。"""

import json
import re
import time
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from general_agent.attachments.service import AttachmentService
from general_agent.memory.service import MemoryService
from general_agent.memory.recall import MemoryRecall
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.provider import ModelFailure, ModelMessage, TokenUsage
from general_agent.prompts import web_query_instruction, web_results_material
from general_agent.infrastructure.settings import SettingsInput, SettingsStore, StoredSettings


class SearchSettingsInput(SettingsInput):
    """固定智谱基础搜索身份，独立凭据与可配置端点沿用安全 URL 校验。"""
    base_url: str = 'https://open.bigmodel.cn/api/paas/v4'
    model: Literal['zhipu-search-std'] = 'zhipu-search-std'


class SearchQueryInput(BaseModel):
    """编辑外发文本必须携带当前修订，不能暗中修改已有批准。"""
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    revision: UUID
    query: str = Field(min_length=1, max_length=70)


class SearchDecisionInput(BaseModel):
    """决定只针对一个已经展示的服务端修订，不接受任意 approved 标志。"""
    model_config = ConfigDict(extra='forbid')
    revision: UUID
    decision: Literal['approve','reject']


class SearchAwaiting(Exception):
    """请求等待具体外发内容的用户决定，不能由其他操作授权替代。"""


def search_query(content: str) -> str | None:
    """只识别本次输入独立句首的明确联网指令，否定、引用和历史材料没有授权。"""
    if re.search(r'(?:不要|不许|禁止|别)(?:实际|真的|再次)?(?:联网|上网|搜索)',content):
        return None
    match=re.fullmatch(r'\s*(?:请)?(?:帮我)?(?:联网|上网)(?:搜索|查询|查找|查一下|查)\s*[：:]?\s*(.+?)\s*',content,re.DOTALL)
    return match.group(1) if match else None


class SearchService:
    """保存一次联网执行的真实状态，通过统一调用网关发送请求并核验实际来源。"""

    def __init__(self, database: Database, calls: CallGateway, settings: SettingsStore, memory: MemoryService, attachments: AttachmentService, transport: httpx.BaseTransport | None = None) -> None:
        """绑定独立搜索设置、受控资料与记忆来源；外发许可不来自模型或检索正文。"""
        self.database, self.calls, self.settings, self.transport = database, calls, settings, transport
        self.memory, self.attachments = memory, attachments
        self.recall = MemoryRecall(memory)

    def prepare(self, conversation_id: UUID, user_id: UUID, content: str, model_settings: StoredSettings, operation: OperationContext) -> list[ModelMessage] | None:
        """明确联网且来源操作仍有效才登记规划；个人资料先持久确认，恢复时复用完成结果。"""
        query = search_query(content)
        if query is None:
            return None
        saved = self.settings.read()
        if saved is None or saved.model != 'zhipu-search-std':
            raise ValueError('尚未配置智谱 Web Search。请在设置中填写独立搜索密钥；本次未联网。')
        with self.database.connect() as connection:
            self.memory.guard_source(connection,conversation_id,user_id,operation)
            connection.execute("INSERT INTO search_requests (id,operation_id,conversation_id,message_id,query,search_revision,status) VALUES (%s,%s,%s,%s,%s,%s,'planning') ON CONFLICT(operation_id) DO NOTHING", (uuid4(),operation.operation_id,conversation_id,user_id,query,saved.revision))
            row = connection.execute('SELECT * FROM search_requests WHERE operation_id=%s', (operation.operation_id,)).fetchone()
        assert row is not None
        if row['search_revision'] != saved.revision:
            raise ValueError('搜索配置已经变化，请停止当前回答后重新发起联网请求。')
        if row['status'] == 'planning':
            materials, sources = self.source_material(conversation_id,user_id,content,model_settings,operation)
            if sources:
                messages: list[ModelMessage] = [{'role':'system','content':web_query_instruction()}]
                messages.extend(materials)
                messages.append({'role':'user','content':query})
                result = self.calls.bind(operation).complete(model_settings,messages)
                try:
                    proposal=json.loads(result.content)
                    query=SearchQueryInput(revision=uuid4(),query=proposal['query']).query
                except (ValueError,KeyError,TypeError):
                    raise ValueError('未生成有效的搜索查询；没有向智谱发送资料，请调整问题后重试。') from None
            elif len(query)>70:
                raise ValueError('智谱搜索查询最多 70 字符，请缩短后重试。')
            with self.database.connect() as connection:
                row=connection.execute("UPDATE search_requests SET query=%s,sources=%s,status=%s,expires_at=%s WHERE id=%s AND status='planning' RETURNING *",(query,Jsonb(sources),'pending' if sources else 'approved',time.time()+1800,row['id'])).fetchone()
            if row is None:
                raise ValueError('搜索规划已停止或失效，迟到的查询不会重新生效。')
        if row['status'] in ('pending','approved') and row['sources'] and time.time()>row['expires_at']:
            self.expire(row,'外发确认已超过 30 分钟，请重新发起搜索。')
        if row['status'] in ('pending','approved','complete') and not self.valid_sources(row):
            self.expire(row,'搜索引用的记忆或附件已经变化，原批准失效；请重新发起搜索。')
        if row['status']=='pending':
            raise SearchAwaiting()
        if row['status'] not in ('approved','complete'):
            raise ValueError('该搜索已经结束或失效，请重新发起。')
        if row['status'] != 'complete':
            try:
                results = self.fetch(saved, str(row['query']), operation)
                with self.database.connect() as connection:
                    for item in results:
                        key = connection.execute("SELECT nextval('web_source_keys') AS value").fetchone()
                        assert key is not None
                        item['key'] = 'W' + str(key['value'])
                    connection.execute("UPDATE search_requests SET status='complete',results=%s,error=NULL WHERE id=%s AND status='approved'", (Jsonb(results), row['id']))
                row['results'] = results
            except Exception as error:
                with self.database.connect() as connection:
                    connection.execute("UPDATE search_requests SET status='failed',error=%s WHERE id=%s AND status='approved'", (str(error) if isinstance(error,ModelFailure) else '联网搜索未完成，可以重试。',row['id']))
                raise
        return [{'role': 'system', 'content': web_results_material(row['results'])}]

    def tool(self, conversation_id: UUID, user_id: UUID, query: str, operation: OperationContext) -> dict[str, Any]:
        """执行模型选择的公开网页查询；校验查询、配置和来源后返回智谱结果，不使用意图正则。"""
        query = query.strip()
        if not query or len(query) > 70:
            raise ValueError('联网搜索词必须为 1 到 70 个字符。')
        saved = self.settings.read()
        if saved is None or saved.model != 'zhipu-search-std':
            raise ValueError('尚未配置智谱 Web Search。')
        with self.database.connect() as connection:
            self.memory.guard_source(connection, conversation_id, user_id, operation)
            connection.execute(
                "INSERT INTO search_requests(id,operation_id,conversation_id,message_id,query,search_revision,status) VALUES(%s,%s,%s,%s,%s,%s,'approved') ON CONFLICT(operation_id) DO NOTHING",
                (uuid4(), operation.operation_id, conversation_id, user_id, query, saved.revision),
            )
            row = connection.execute('SELECT * FROM search_requests WHERE operation_id=%s', (operation.operation_id,)).fetchone()
        assert row is not None
        if row['search_revision'] != saved.revision or row['query'] != query:
            raise ValueError('本轮已经使用了其他联网查询，不能暗中改写。')
        if row['status'] != 'complete':
            results = self.fetch(saved, query, operation)
            with self.database.connect() as connection:
                for item in results:
                    key = connection.execute("SELECT nextval('web_source_keys') AS value").fetchone()
                    assert key is not None
                    item['key'] = 'W' + str(key['value'])
                connection.execute("UPDATE search_requests SET status='complete',results=%s,error=NULL WHERE id=%s", (Jsonb(results), row['id']))
            row['results'] = results
        return {'query': query, 'results': row['results']}

    def source_material(self, conversation_id: UUID, user_id: UUID, content: str, settings: StoredSettings, operation: OperationContext) -> tuple[list[ModelMessage],list[dict[str,Any]]]:
        """仅为当前选定、具名或明确指向个人资料的引用准备材料；普通主题词不授权读取附件或长期记忆。"""
        materials: list[ModelMessage] = []
        sources: list[dict[str,Any]] = []
        if re.search(r'(?:根据|结合|参考|基于|使用|读取)(?:我的|已保存的)?(?:长期)?记忆(?=[里中，,：:\s]|来|查|搜索|检索)|(?:我的|已保存的|之前保存的)(?:长期)?记忆(?=[里中的，,：:\s]|来|查|搜索|检索|$)|你(?:之前|曾经|已经)?(?:帮我|为我)?记住的|(?:长期)?记忆(?:里|中)(?:保存的|的)?(?:我的|住址|地址|偏好|职业)|我的(?:偏好|住址|地址|职业|情况)|我住|适合我|我附近|我家|我所在',content):
            materials.extend(self.recall.prepare(user_id,content,operation,None))
            checkpoint=self.recall.checkpoint(operation)
            matches=checkpoint['matches'] if checkpoint and checkpoint['status']=='complete' else []
            if not matches:
                raise ValueError('没有找到可用的相关长期记忆来生成搜索词。请直接写出要搜索的内容；本次未联网。')
            sources.extend({'kind':'memory','id':item['action_id'],'version':item['version'],'label':'相关长期记忆'} for item in matches)
        scope=self.attachments.question_scope(conversation_id,user_id,content,[])
        referenced=re.search(
            r'(?:根据|结合|基于|使用|读取)(?:附件|文件|资料|截图|图表|图片)(?=里|中|上|来|查|搜索|检索|分析|回答|[，,：:\s]|$)'
            r'|(?:这份|这些|这张|所选|我(?:刚)?上传的|已上传的|当前的?)(?:附件|文件|资料|截图|图表|图片)'
            r'|(?:附件|文件|资料|截图|图表|图片)(?:里|中|上)',content)
        if scope or referenced:
            material=self.attachments.prepare(conversation_id,user_id,content,settings=settings)
            if not material.citations:
                raise ValueError('没有找到当前会话已发送且可用的附件资料，请先上传并随消息发送；本次未联网。')
            materials.extend(material.messages)
            identifiers=list(dict.fromkeys(item['attachment_id'] for item in material.citations))
            with self.database.connect() as connection:
                for identifier in identifiers:
                    item=connection.execute("SELECT id,filename,created_at FROM attachments WHERE id=%s AND conversation_id=%s AND status='ready'",(identifier,conversation_id)).fetchone()
                    if item:
                        sources.append({'kind':'attachment','id':str(item['id']),'version':item['created_at'].isoformat(),'label':item['filename']})
        return materials,sources

    def valid_sources(self, request: dict[str,Any]) -> bool:
        """重新核对批准所绑定的来源有效性；删除、替换或更正不能继承旧批准。"""
        with self.database.connect() as connection:
            for source in request['sources']:
                if source['kind']=='memory':
                    row=connection.execute("SELECT id FROM memory_actions WHERE id=%s AND version=%s AND status='complete' AND memory_id IS NOT NULL",(source['id'],source['version'])).fetchone()
                else:
                    row=connection.execute("SELECT id FROM attachments WHERE id=%s AND conversation_id=%s AND created_at=%s AND status='ready'",(source['id'],request['conversation_id'],source['version'])).fetchone()
                if row is None:
                    return False
        return True

    def expire(self, row: dict[str,Any], detail: str) -> None:
        """使失效许可及派生材料不可继续使用，并向调用方返回可解释原因。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE search_requests SET status='expired',error=%s,query='',results='[]' WHERE id=%s",(detail,row['id']))
            connection.execute("UPDATE messages SET status='failed',error=%s WHERE operation_id=%s AND status='paused'",(detail,row['operation_id']))
        raise ValueError(detail)

    def change_query(self, conversation_id: UUID, request_id: UUID, body: SearchQueryInput) -> None:
        """只编辑仍待确认的当前修订，生成新修订后必须重新明确批准。"""
        with self.database.connect() as connection:
            row=connection.execute('SELECT * FROM search_requests WHERE id=%s AND conversation_id=%s FOR UPDATE',(request_id,conversation_id)).fetchone()
            if row is None:
                raise LookupError('本会话没有该搜索确认。')
            if row['status']!='pending' or row['revision']!=body.revision:
                raise ValueError('搜索确认已经变化，请刷新后核对。')
            connection.execute('UPDATE search_requests SET query=%s,revision=%s,expires_at=%s WHERE id=%s',(body.query,uuid4(),time.time()+1800,request_id))

    def decide(self, conversation_id: UUID, request_id: UUID, body: SearchDecisionInput) -> UUID | None:
        """一次决定绑定会话、精确查询修订、来源和有效期，重复提交不会再次启动调用。"""
        with self.database.connect() as connection:
            row=connection.execute('SELECT s.*,m.id AS answer_id,m.status AS answer_status,o.cancelled FROM search_requests s JOIN messages m ON m.operation_id=s.operation_id JOIN cloud_operations o ON o.id=s.operation_id WHERE s.id=%s AND s.conversation_id=%s FOR UPDATE OF s',(request_id,conversation_id)).fetchone()
            if row is None:
                raise LookupError('本会话没有该搜索确认。')
            if row['revision']!=body.revision:
                raise ValueError('查询内容已经修改，旧确认已失效，请核对当前文本。')
            if row['status'] in ('approved','complete') and body.decision=='approve':
                return None
            if row['status']=='rejected' and body.decision=='reject':
                return None
            if row['status']!='pending' or row['cancelled'] or row['answer_status']!='paused':
                raise ValueError('搜索确认已结束或失效，请重新发起。')
            invalid=time.time()>row['expires_at'] or not self.valid_sources(row)
            if not invalid:
                status='approved' if body.decision=='approve' else 'rejected'
                connection.execute('UPDATE search_requests SET status=%s WHERE id=%s',(status,request_id))
                if body.decision=='reject':
                    connection.execute("UPDATE messages SET status='complete',content='已拒绝发送这些搜索内容，本次没有联网。' WHERE id=%s",(row['answer_id'],))
                    connection.execute('UPDATE cloud_operations SET cancelled=TRUE WHERE id=%s',(row['operation_id'],))
                    return None
                return UUID(str(row['answer_id']))
        self.expire(row,'搜索确认已过期或来源已经变化，请重新发起并核对具体文本。')
        return None

    def fail(self, operation: OperationContext, detail: str) -> None:
        """查询规划或配置检查失败时结束未发出的步骤，避免界面仍显示正在准备。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE search_requests SET status='failed',error=%s WHERE operation_id=%s AND status IN ('planning','pending','approved')",(detail,operation.operation_id))

    def cancel(self, operation_id: UUID) -> None:
        """停止执行时撤销尚未使用的搜索许可，不能用旧确认恢复已取消操作。"""
        with self.database.connect() as connection:
            connection.execute("UPDATE search_requests SET status='cancelled' WHERE operation_id=%s AND status IN ('planning','pending','approved')",(operation_id,))

    def fetch(self, settings: StoredSettings, query: str, operation: OperationContext) -> list[dict[str, Any]]:
        """以智谱基础引擎发送精确查询，关联回答停止状态并记录实际发出的请求次数。"""
        with self.database.connect() as connection:
            answer=connection.execute("SELECT id FROM messages WHERE operation_id=%s AND role='assistant'",(operation.operation_id,)).fetchone()
        with self.calls.bind(operation).call(settings, 'credits', assistant_message_id=answer['id'] if answer else None) as call_id:
            try:
                with httpx.Client(transport=self.transport, timeout=30, follow_redirects=False, trust_env=False) as client:
                    response = client.post(settings.base_url.rstrip('/') + '/web_search', headers={'Authorization': 'Bearer ' + settings.api_key}, json={'search_query': query, 'search_engine': 'search_std', 'search_intent': False, 'count': 5, 'search_recency_filter': 'noLimit', 'content_size': 'medium', 'request_id': str(operation.operation_id)})
            except httpx.TimeoutException:
                raise ModelFailure('智谱搜索超时，本次未取得可核验结果。') from None
            except httpx.RequestError:
                raise ModelFailure('无法连接智谱搜索服务，本次未取得可核验结果。') from None
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            # 搜索响应不返回 token，用 credits 只记录实际发出的请求次数。
            self.calls.operations.record(call_id, TokenUsage(), credits=Decimal(1))
            if not response.is_success:
                raise ModelFailure(f'智谱搜索失败（HTTP {response.status_code}），本次未取得可核验结果；请检查搜索设置或稍后重试。')
            if not isinstance(payload,dict) or not isinstance(payload.get('search_result'),list):
                raise ModelFailure('智谱未返回有效结果列表，本次未完成搜索。')
            results = []
            for item in payload['search_result'][:5]:
                if not isinstance(item,dict) or not isinstance(item.get('link'),str):
                    continue
                url = item['link']
                try:
                    parts = urlsplit(url)
                    valid = parts.scheme in ('http','https') and parts.hostname and not parts.username and not parts.password and not any(ord(char)<33 for char in url)
                except ValueError:
                    valid = False
                if valid:
                    results.append({'url': url, 'title': str(item.get('title',''))[:500], 'excerpt': str(item.get('content',''))[:8000]})
            return results

    def resolve(self, operation: OperationContext, answer: str) -> tuple[str,list[dict[str, Any]]]:
        """只显示本次实际安全来源；漏引时仍提供核对链接，空结果不得伪称已经查证。"""
        with self.database.connect() as connection:
            row = connection.execute("SELECT results FROM search_requests WHERE operation_id=%s AND status='complete'", (operation.operation_id,)).fetchone()
        if row is None:
            return answer, []
        if not row['results']:
            return ('本次搜索未找到可核验的网页结果，无法基于网络来源回答。请调整关键词后重试。' if answer else answer), []
        allowed = {item['key']: item for item in row['results']}
        used = []

        def replace(match: re.Match[str]) -> str:
            """把未提供的网页标识替换为明确的未核验提示。"""
            key = match.group(1)
            if key not in allowed:
                return '[网页出处未核验]'
            if allowed[key] not in used:
                used.append(allowed[key])
            return match.group(0)

        cleaned = re.sub(r'\[(W\d+)\]', replace, answer)
        if answer and allowed and not used:
            cleaned += '\n\n联网提示：此回答缺少可核验的网页引用，请核对搜索来源。'
            used = list(allowed.values())
        return cleaned, used

    def save_sources(self, message_id: UUID, operation: OperationContext) -> None:
        """与来源删除的操作失效串行发布出处，迟到结果不能恢复已清空的网页来源。"""
        with self.database.connect() as connection:
            active=connection.execute('SELECT invalidated FROM cloud_operations WHERE id=%s FOR SHARE',(operation.operation_id,)).fetchone()
            if active is None or active['invalidated']:
                return
            row = connection.execute('SELECT content FROM messages WHERE id=%s',(message_id,)).fetchone()
            if row is None:
                return
            content, sources = self.resolve(operation, str(row['content']))
            connection.execute("UPDATE messages SET content=CASE WHEN status='stopped' THEN content ELSE %s END,web_sources=%s WHERE id=%s",(content,Jsonb(sources),message_id))
