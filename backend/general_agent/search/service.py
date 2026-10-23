"""DeepSeek Search / Fetch 的模型工具、操作记录与来源核验。"""

import re
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from general_agent.attachments.service import AttachmentService
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelFailure, TokenUsage
from general_agent.infrastructure.settings import SettingsInput, SettingsStore, StoredSettings
from general_agent.memory.service import MemoryService
from general_agent.prompts import TOOL_DESCRIPTIONS


class SearchSettingsInput(SettingsInput):
    """固定 DeepSeek 搜索与抓取身份，独立凭据与可配置端点沿用安全 URL 校验。"""

    base_url: str = 'https://api.deepseek.com'
    model: Literal['deepseek-search'] = 'deepseek-search'


class SearchWebInput(BaseModel):
    """限制模型生成的公开网页查询长度，避免无效或超限请求。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=128)


class FetchWebInput(BaseModel):
    """校验交给 DeepSeek 抓取的网页地址，不在本机访问目标网站。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid', strict=True)
    url: str = Field(min_length=1, max_length=8192)

    @field_validator('url')
    @classmethod
    def validate_url(cls, value: str) -> str:
        """只允许带主机且不含凭据或控制字符的 HTTP(S) 地址。"""
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or any(ord(char) < 33 for char in value)):
            raise ValueError('请提供不含凭据的 HTTP 或 HTTPS 网页地址。')
        return value


def search_web_tool(name: str = "search_web") -> dict[str, Any]:
    """返回搜索或网页抓取工具的 OpenAI 兼容函数定义。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": TOOL_DESCRIPTIONS[name],
            "parameters": (FetchWebInput if name == "fetch_web" else SearchWebInput).model_json_schema(),
        },
    }


class SearchService:
    """保存联网执行事实，通过统一调用网关发送请求并核验实际来源。"""

    def __init__(
        self,
        database: Database,
        calls: CallGateway,
        settings: SettingsStore,
        memory: MemoryService,
        attachments: AttachmentService,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """绑定独立搜索设置、来源守卫与受控 HTTP 传输。"""
        self.database, self.calls, self.settings, self.memory = database, calls, settings, memory
        self.attachments, self.transport = attachments, transport

    def tool(
        self,
        conversation_id: UUID,
        user_id: UUID,
        query: str,
        operation: OperationContext,
        source_attachment_ids: list[UUID] | None = None,
        *,
        kind: Literal["search", "fetch"] = "search",
    ) -> dict[str, Any]:
        """允许本轮执行多个查询，按查询或 URL 复用结果并累计来源及停止状态。"""
        query = (SearchWebInput(query=query).query if kind == "search"
                 else FetchWebInput(url=query).url)
        saved = self.settings.read()
        if saved is None or saved.model != 'deepseek-search':
            raise ValueError('尚未配置 DeepSeek Search / Fetch。请在管理端填写 DeepSeek 搜索密钥。')
        account_id = self.memory.account_id_for_conversation(conversation_id)
        with self.database.connect() as connection:
            self.memory.guard_source(connection, conversation_id, user_id, operation)
            attachment_sources = connection.execute(
                "SELECT id,filename,created_at FROM attachments "
                "WHERE conversation_id=%s AND status='ready' ORDER BY created_at,id",
                (conversation_id,),
            ).fetchall()
            memory_sources = connection.execute(
                "SELECT o.action_id,a.version FROM memory_tool_observations o "
                "JOIN memory_actions a ON a.id=o.action_id "
                "WHERE o.message_id=%s AND a.status='complete'",
                (user_id,),
            ).fetchall()
            sources = [
                {
                    'kind': 'attachment',
                    'id': str(item['id']),
                    'version': item['created_at'].isoformat(),
                    'label': item['filename'],
                }
                for item in attachment_sources
            ]
            sources.extend({
                'kind': 'memory',
                'id': str(item['action_id']),
                'version': item['version'],
                'label': '相关长期记忆',
            } for item in memory_sources)
            connection.execute(
                "INSERT INTO search_requests(id,operation_id,conversation_id,user_id,message_id,query,search_revision,status,sources) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,'approved',%s) ON CONFLICT(operation_id) DO NOTHING",
                (uuid4(), operation.operation_id, conversation_id, account_id, user_id, query if kind == "search" else "", saved.revision, Jsonb(sources)),
            )
            row = connection.execute('SELECT * FROM search_requests WHERE operation_id=%s', (operation.operation_id,)).fetchone()
        assert row is not None
        if row['search_revision'] != saved.revision:
            raise ValueError('搜索配置已经变化，请停止当前回答后重新发送。')
        cached = [item for item in row['results']
                  if (kind == 'search' and item.get('kind', 'search') == 'search' and item.get('query') == query)
                  or (kind == 'fetch' and item.get('kind') == 'fetch' and item['url'] == query)]
        if row['status'] in ('complete', 'failed') and (cached or (kind == 'search' and query in row['completed_queries'])):
            return {'query': query, 'results': cached}
        if row['status'] in ('cancelled', 'rejected', 'expired'):
            raise ValueError('该搜索已经结束或失效，请重新发送问题。')
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE search_requests SET status='approved',error=NULL,query=CASE WHEN %s THEN %s ELSE query END "
                "WHERE id=%s AND status IN ('approved','failed','complete')",
                (kind == "search", query, row['id']),
            )
        try:
            results = self.fetch(saved, query, operation, kind)
        except Exception as error:
            detail = str(error) if isinstance(error, ModelFailure) else '联网搜索未完成，可以重试。'
            self.fail_request(operation, detail)
            raise
        with self.database.connect() as connection:
            active = connection.execute('SELECT cancelled FROM cloud_operations WHERE id=%s', (operation.operation_id,)).fetchone()
            if active is None or active['cancelled']:
                raise ValueError('原操作已停止，迟到的搜索结果不会继续使用。')
            for item in results:
                item['kind'] = kind
                if kind == 'search':
                    item['query'] = query
                key = connection.execute("SELECT nextval('web_source_keys') AS value").fetchone()
                assert key is not None
                item['key'] = 'W' + str(key['value'])
            updated = connection.execute(
                "UPDATE search_requests SET status='complete',results=results || %s,error=NULL, "
                "completed_queries=completed_queries || CASE WHEN %s THEN ARRAY[%s]::text[] ELSE ARRAY[]::text[] END "
                "WHERE id=%s AND status='approved' RETURNING id",
                (Jsonb(results), kind == 'search', query, row['id']),
            ).fetchone()
            if updated is None:
                raise ValueError('搜索操作已经停止或失效，结果不会继续使用。')
        return {'query': query, 'results': results}

    def fail_request(self, operation: OperationContext, detail: str) -> None:
        """把一个真实失败的搜索请求标记为失败，不伪造网页来源。"""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE search_requests SET status='failed',error=%s "
                "WHERE operation_id=%s AND status IN ('planning','pending','approved')",
                (detail, operation.operation_id),
            )

    def fail(self, operation: OperationContext, detail: str) -> None:
        """回答整体失败时结束对该搜索记录的等待，避免界面显示仍在执行。"""
        self.fail_request(operation, detail)

    def cancel(self, operation_id: UUID) -> None:
        """停止执行时撤销尚未完成的搜索记录。"""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE search_requests SET status='cancelled' "
                "WHERE operation_id=%s AND status IN ('planning','pending','approved')",
                (operation_id,),
            )

    def fetch(
        self, settings: StoredSettings, query: str, operation: OperationContext,
        kind: Literal["search", "fetch"] = "search",
    ) -> list[dict[str, Any]]:
        """调用 DeepSeek Search/Fetch，按成功且非空的官方规则记录 credits。"""
        with self.database.connect() as connection:
            answer = connection.execute(
                "SELECT id FROM messages WHERE operation_id=%s AND role='assistant'",
                (operation.operation_id,),
            ).fetchone()
        base = settings.base_url.rstrip('/')
        # 同时兼容官方根地址和常用的 /v1 地址，避免重复版本前缀。
        endpoint = base + ('/tools/' if base.endswith('/v1') else '/v1/tools/') + kind
        body = {'query': query, 'count': 5, 'need_content': False} if kind == 'search' else {'url': query}
        with self.calls.bind(operation).call(settings, 'credits', assistant_message_id=answer['id'] if answer else None) as call_id:
            try:
                with httpx.Client(transport=self.transport, timeout=30, follow_redirects=False, trust_env=False) as client:
                    response = client.post(endpoint, headers={'Authorization': 'Bearer ' + settings.api_key}, json=body)
            except httpx.TimeoutException:
                raise ModelFailure('DeepSeek 联网请求超时，本次未取得可核验结果。') from None
            except httpx.RequestError:
                raise ModelFailure('无法连接 DeepSeek 联网服务，本次未取得可核验结果。') from None
            if response.status_code != 200:
                self.calls.operations.record(call_id, TokenUsage(), credits=Decimal(0))
                raise ModelFailure(f'DeepSeek 联网请求失败（HTTP {response.status_code}），请检查配置或稍后重试。')
            try:
                payload = response.json()
            except ValueError:
                raise ModelFailure('DeepSeek 返回了无效 JSON，本次未完成请求。') from None
            if not isinstance(payload, dict):
                raise ModelFailure('DeepSeek 返回了无效结果，本次未完成请求。')
            if kind == 'fetch':
                if not isinstance(payload.get('content'), str) or not isinstance(payload.get('title'), str):
                    raise ModelFailure('DeepSeek 未返回有效网页正文，本次未完成抓取。')
                content = payload['content']
                self.calls.operations.record(call_id, TokenUsage(), credits=Decimal(int(bool(content.strip()))))
                if not content.strip():
                    raise ModelFailure('DeepSeek 未返回可读取的网页正文。')
                return [{'url': query, 'title': payload['title'], 'excerpt': content[:8000], 'content': content}]
            if not isinstance(payload.get('results'), list):
                raise ModelFailure('DeepSeek 未返回有效结果列表，本次未完成搜索。')
            self.calls.operations.record(call_id, TokenUsage(), credits=Decimal(int(bool(payload['results']))))
            results = []
            for item in payload['results'][:5]:
                if not isinstance(item, dict) or not isinstance(item.get('url'), str):
                    continue
                try:
                    url = FetchWebInput(url=item['url']).url
                except ValueError:
                    continue
                results.append({
                    'url': url,
                    'title': str(item.get('title', ''))[:500],
                    'excerpt': str(item.get('summary') or item.get('snippet') or '')[:8000],
                })
            return results

    def resolve(self, operation: OperationContext, answer: str) -> tuple[str, list[dict[str, Any]]]:
        """核对搜索和抓取来源编号；保留失败前取得的来源，空搜索不得伪称已查证。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT results,status FROM search_requests WHERE operation_id=%s AND status IN ('complete','failed')",
                (operation.operation_id,),
            ).fetchone()
        if row is None or (row['status'] == 'failed' and not row['results']):
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
