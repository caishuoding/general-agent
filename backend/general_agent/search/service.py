"""Zhipu Web Search / Reader 的模型工具、操作记录与来源核验。"""

import random
import re
import time
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
from general_agent.infrastructure.operations import CallBudgetExceeded, OperationContext
from general_agent.infrastructure.provider import ModelFailure, TokenUsage
from general_agent.infrastructure.settings import SettingsInput, SettingsStore, StoredSettings
from general_agent.memory.service import MemoryService
from general_agent.prompts import TOOL_DESCRIPTIONS
from general_agent.search.failures import WEB_MAX_ATTEMPTS, WEB_MAX_RETRY_WAIT, WebRequestFailure, http_failure


SEARCH_CANDIDATE_COUNT = 15
SEARCH_RESULT_LIMIT = 8
MAX_RESULTS_PER_DOMAIN = 2


class SearchSettingsInput(SettingsInput):
    """固定智谱搜索与网页读取身份，独立凭据与可配置端点沿用安全 URL 校验。"""

    base_url: str = 'https://open.bigmodel.cn/api/paas/v4'
    model: Literal['zhipu-web'] = 'zhipu-web'


class SearchWebInput(BaseModel):
    """校验智谱 Web Search 的关键词查询。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid', strict=True)
    query: str = Field(
        min_length=1,
        max_length=70,
        description='关键词化搜索表达，智谱建议不超过 70 字；不要填写 URL。',
    )


class FetchWebInput(BaseModel):
    """校验交给智谱 Reader 抓取的网页地址，不在本机访问目标网站。"""

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


def normalize_search_result(item: dict[str, Any], order: int) -> dict[str, Any] | None:
    """保留智谱官方结果的标题、正文摘要、站点和日期，不臆造质量分数。"""
    if not isinstance(item.get('link'), str):
        return None
    try:
        url = FetchWebInput(url=item['link']).url
    except ValueError:
        return None
    title = item.get('title')
    summary = item.get('content') or ''
    site_name = item.get('media')
    date = item.get('publish_date')
    return {
        'url': url,
        'title': str(title or '')[:500],
        'excerpt': str(summary)[:8000],
        'site_name': str(site_name or urlsplit(url).hostname or '')[:200],
        'date': str(date)[:100] if isinstance(date, str) and date.strip() else None,
        '_order': order,
    }


def rank_search_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保持供应商相关性顺序，并限制单一域名占满返回结果。"""
    selected: list[dict[str, Any]] = []
    domain_counts: dict[str, int] = {}
    for item in results:
        domain = (urlsplit(item['url']).hostname or '').lower().removeprefix('www.')
        if domain and domain_counts.get(domain, 0) >= MAX_RESULTS_PER_DOMAIN:
            continue
        selected.append(item)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        if len(selected) >= SEARCH_RESULT_LIMIT:
            break
    for item in selected:
        item.pop('_order', None)
    return selected


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
        user_message_id: UUID,
        query: str,
        operation: OperationContext,
        *,
        kind: Literal["search", "fetch"] = "search",
    ) -> dict[str, Any]:
        """执行搜索或抓取请求，校验参数并复用本轮已有结果。"""
        if kind == 'search':
            query = SearchWebInput(query=query).query
        else:
            query = FetchWebInput(url=query).url
        saved = self.settings.read()
        if saved is None or saved.model != 'zhipu-web':
            raise ValueError('尚未配置 Zhipu Web Search / Reader。请在管理端填写智谱搜索密钥。')
        account_id = self.memory.account_id_for_conversation(conversation_id)
        with self.database.connect() as connection:
            self.memory.guard_source(connection, conversation_id, user_message_id, operation)
            attachment_sources = connection.execute(
                "SELECT id,filename,created_at FROM attachments "
                "WHERE conversation_id=%s AND status='ready' ORDER BY created_at,id",
                (conversation_id,),
            ).fetchall()
            memory_sources = connection.execute(
                "SELECT o.action_id,a.version FROM memory_tool_observations o "
                "JOIN memory_actions a ON a.id=o.action_id "
                "WHERE o.message_id=%s AND a.status='complete'",
                (user_message_id,),
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
                (uuid4(), operation.operation_id, conversation_id, account_id, user_message_id, query if kind == "search" else "", saved.revision, Jsonb(sources)),
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
        if row['status'] == 'cancelled':
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
                "WHERE operation_id=%s AND status='approved'",
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
                "WHERE operation_id=%s AND status='approved'",
                (operation_id,),
            )

    def fetch(
        self, settings: StoredSettings, query: str, operation: OperationContext,
        kind: Literal["search", "fetch"] = "search",
    ) -> list[dict[str, Any]]:
        """仅对临时网络故障执行初次加两次退避重试，每次外发独立鉴权、计数和计量。"""
        for attempt in range(WEB_MAX_ATTEMPTS):
            try:
                return self.fetch_once(settings, query, operation, kind)
            except CallBudgetExceeded as error:
                failure = WebRequestFailure(str(error), retryable=False, scope='execution', error_code='call_budget_exhausted')
                failure.attempt_count = attempt
                raise failure from None
            except WebRequestFailure as error:
                error.attempt_count = attempt + 1
                if not error.retryable or attempt + 1 == WEB_MAX_ATTEMPTS:
                    raise
                delay = max(0.5 * (2 ** attempt) + random.uniform(0, 0.25), error.retry_after)
                if delay > WEB_MAX_RETRY_WAIT:
                    # 不提前违背服务端等待时间，也不让一次重试无限占用工作线程。
                    raise
                self.wait_for_retry(operation, settings, delay)
        raise AssertionError('联网重试循环没有返回结果或失败。')

    def wait_for_retry(self, operation: OperationContext, settings: StoredSettings, delay: float) -> None:
        """小步等待并检查取消、来源撤销和配置版本，停止后不再发送下一次请求。"""
        deadline = time.monotonic() + delay
        while True:
            with self.database.connect() as connection:
                row = connection.execute('SELECT cancelled,invalidated FROM cloud_operations WHERE id=%s', (operation.operation_id,)).fetchone()
            if row is None or row['cancelled'] or row['invalidated']:
                raise ValueError('操作已停止，已取消后续联网重试。')
            current = self.settings.read()
            if current is None or current.revision != settings.revision:
                raise ValueError('搜索配置已经变化，已取消后续联网重试。')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.1))

    def fetch_once(
        self, settings: StoredSettings, query: str, operation: OperationContext,
        kind: Literal['search', 'fetch'],
    ) -> list[dict[str, Any]]:
        """发送恰好一次真实请求，按官方 HTTP 状态分类失败并保留安全诊断字段。"""
        with self.database.connect() as connection:
            answer = connection.execute(
                "SELECT id FROM messages WHERE operation_id=%s AND role='assistant'",
                (operation.operation_id,),
            ).fetchone()
        base = settings.base_url.rstrip('/')
        endpoint = base + ('/web_search' if kind == 'search' else '/reader')
        if kind == 'search':
            body = {
                'search_query': query,
                'search_engine': 'search_pro',
                'search_intent': False,
                'count': SEARCH_CANDIDATE_COUNT,
                'content_size': 'high',
            }
        else:
            body = {'url': query, 'return_format': 'markdown', 'retain_images': False}
        with self.calls.bind(operation).call(settings, 'credits', assistant_message_id=answer['id'] if answer else None,
                                             web_kind=kind) as call_id:
            try:
                with httpx.Client(transport=self.transport, timeout=30, follow_redirects=False, trust_env=False) as client:
                    response = client.post(endpoint, headers={'Authorization': 'Bearer ' + settings.api_key}, json=body)
            except httpx.TimeoutException:
                raise WebRequestFailure('智谱联网请求超时，本次未取得可核验结果。', retryable=True) from None
            except httpx.RequestError:
                raise WebRequestFailure('无法连接智谱联网服务，本次未取得可核验结果。', retryable=True) from None
            if response.status_code != 200:
                self.calls.operations.record(call_id, TokenUsage(), credits=Decimal(0))
                raise http_failure(response)
            try:
                payload = response.json()
            except ValueError:
                raise WebRequestFailure('智谱返回了无效 JSON，本次未完成请求。', retryable=False) from None
            if not isinstance(payload, dict):
                raise WebRequestFailure('智谱返回了无效结果，本次未完成请求。', retryable=False)
            if kind == 'fetch':
                result = payload.get('reader_result')
                if not isinstance(result, dict) or not isinstance(result.get('content'), str) or not isinstance(result.get('title'), str):
                    raise WebRequestFailure('智谱未返回有效网页正文，本次未完成抓取。', retryable=False)
                content = result['content']
                self.calls.operations.record(call_id, TokenUsage(), credits=Decimal(int(bool(content.strip()))))
                if not content.strip():
                    raise WebRequestFailure('智谱未返回可读取的网页正文。', retryable=False)
                return [{'url': query, 'title': result['title'], 'excerpt': content[:8000], 'content': content}]
            if not isinstance(payload.get('search_result'), list):
                raise WebRequestFailure('智谱未返回有效结果列表，本次未完成搜索。', retryable=False)
            self.calls.operations.record(call_id, TokenUsage(), credits=Decimal(int(bool(payload['search_result']))))
            results = [
                normalized
                for order, item in enumerate(payload['search_result'][:50])
                if isinstance(item, dict)
                and (normalized := normalize_search_result(item, order)) is not None
            ]
            return rank_search_results(results)

    def resolve(self, operation: OperationContext, answer: str) -> tuple[str, list[dict[str, Any]]]:
        """核对主执行及已交付子任务的真实网页来源，拒绝未知编号。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT results,status FROM search_requests WHERE operation_id=%s AND status IN ('complete','failed')",
                (operation.operation_id,),
            ).fetchone()
            delivered = connection.execute("SELECT s.payload FROM agent_sources s JOIN agent_runs r ON r.id=s.run_id JOIN agents a ON a.id=r.agent_id WHERE a.answer_attempt_id IN (SELECT root.answer_attempt_id FROM agents root JOIN agent_runs main ON main.agent_id=root.id WHERE main.operation_id=%s AND root.parent_agent_id IS NULL) AND (a.parent_agent_id IS NULL OR s.delivered) AND s.source_key ~ '^W[0-9]+$'", (operation.operation_id,)).fetchall()
        if delivered:
            combined = {item['key']: item for item in (row['results'] if row else [])}
            combined.update({item['payload']['key']: item['payload'] for item in delivered})
            row = {'status': 'complete', 'results': list(combined.values())}
        if row is None or (row['status'] == 'failed' and not row['results']):
            return re.sub(r'\[(W\d+)\]', '[网页出处未核验]', answer), []
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
        return cleaned, used
