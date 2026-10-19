"""智谱 Web Search 的模型工具、操作记录与来源核验。"""

import re
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from general_agent.attachments.service import AttachmentService
from general_agent.infrastructure.calls import CallGateway
from general_agent.infrastructure.database import Database
from general_agent.infrastructure.operations import OperationContext
from general_agent.infrastructure.provider import ModelFailure, TokenUsage
from general_agent.infrastructure.settings import SettingsInput, SettingsStore, StoredSettings
from general_agent.memory.service import MemoryService
from general_agent.prompts import TOOL_DESCRIPTIONS


class SearchSettingsInput(SettingsInput):
    """固定智谱基础搜索身份，独立凭据与可配置端点沿用安全 URL 校验。"""

    base_url: str = 'https://open.bigmodel.cn/api/paas/v4'
    model: Literal['zhipu-search-std'] = 'zhipu-search-std'


class SearchWebInput(BaseModel):
    """限制模型生成的公开网页查询长度，避免无效或超限请求。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=70)


def search_web_tool() -> dict[str, Any]:
    """返回搜索工具的 OpenAI 兼容函数定义。"""
    return {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": TOOL_DESCRIPTIONS["search_web"],
            "parameters": SearchWebInput.model_json_schema(),
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
    ) -> dict[str, Any]:
        """直接执行模型选择的公开网页查询，并保存可核验的操作记录。"""
        validated = SearchWebInput.model_validate({"query": query})
        query = validated.query
        saved = self.settings.read()
        if saved is None or saved.model != 'zhipu-search-std':
            raise ValueError('尚未配置智谱 Web Search。请在设置中填写独立搜索密钥。')
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
                (uuid4(), operation.operation_id, conversation_id, account_id, user_id, query, saved.revision, Jsonb(sources)),
            )
            row = connection.execute('SELECT * FROM search_requests WHERE operation_id=%s', (operation.operation_id,)).fetchone()
        assert row is not None
        if row['search_revision'] != saved.revision:
            raise ValueError('搜索配置已经变化，请停止当前回答后重新发送。')
        if row['query'] != query:
            raise ValueError('本轮已经使用了其他联网查询，不能在同一次回答中改写。')
        if row['status'] == 'complete':
            return {'query': query, 'results': row['results']}
        if row['status'] in ('cancelled', 'rejected', 'expired'):
            raise ValueError('该搜索已经结束或失效，请重新发送问题。')
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE search_requests SET status='approved',error=NULL WHERE id=%s AND status IN ('approved','failed')",
                (row['id'],),
            )
        try:
            results = self.fetch(saved, query, operation)
        except Exception as error:
            detail = str(error) if isinstance(error, ModelFailure) else '联网搜索未完成，可以重试。'
            self.fail_request(operation, detail)
            raise
        with self.database.connect() as connection:
            active = connection.execute('SELECT cancelled FROM cloud_operations WHERE id=%s', (operation.operation_id,)).fetchone()
            if active is None or active['cancelled']:
                raise ValueError('原操作已停止，迟到的搜索结果不会继续使用。')
            for item in results:
                key = connection.execute("SELECT nextval('web_source_keys') AS value").fetchone()
                assert key is not None
                item['key'] = 'W' + str(key['value'])
            updated = connection.execute(
                "UPDATE search_requests SET status='complete',results=%s,error=NULL "
                "WHERE id=%s AND status='approved' RETURNING id",
                (Jsonb(results), row['id']),
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

    def fetch(self, settings: StoredSettings, query: str, operation: OperationContext) -> list[dict[str, Any]]:
        """以智谱基础引擎发送精确查询，关联回答停止状态并记录实际请求次数。"""
        with self.database.connect() as connection:
            answer = connection.execute(
                "SELECT id FROM messages WHERE operation_id=%s AND role='assistant'",
                (operation.operation_id,),
            ).fetchone()
        with self.calls.bind(operation).call(settings, 'credits', assistant_message_id=answer['id'] if answer else None) as call_id:
            try:
                with httpx.Client(transport=self.transport, timeout=30, follow_redirects=False, trust_env=False) as client:
                    response = client.post(
                        settings.base_url.rstrip('/') + '/web_search',
                        headers={'Authorization': 'Bearer ' + settings.api_key},
                        json={
                            'search_query': query,
                            'search_engine': 'search_std',
                            'search_intent': False,
                            'count': 5,
                            'search_recency_filter': 'noLimit',
                            'content_size': 'medium',
                            'request_id': str(operation.operation_id),
                        },
                    )
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
            if not isinstance(payload, dict) or not isinstance(payload.get('search_result'), list):
                raise ModelFailure('智谱未返回有效结果列表，本次未完成搜索。')
            results = []
            for item in payload['search_result'][:5]:
                if not isinstance(item, dict) or not isinstance(item.get('link'), str):
                    continue
                url = item['link']
                try:
                    parts = urlsplit(url)
                    valid = (
                        parts.scheme in ('http', 'https')
                        and parts.hostname
                        and not parts.username
                        and not parts.password
                        and not any(ord(char) < 33 for char in url)
                    )
                except ValueError:
                    valid = False
                if valid:
                    results.append({
                        'url': url,
                        'title': str(item.get('title', ''))[:500],
                        'excerpt': str(item.get('content', ''))[:8000],
                    })
            return results

    def resolve(self, operation: OperationContext, answer: str) -> tuple[str, list[dict[str, Any]]]:
        """只显示本次实际安全来源；漏引时仍提供核对链接，空结果不得伪称已经查证。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT results FROM search_requests WHERE operation_id=%s AND status='complete'",
                (operation.operation_id,),
            ).fetchone()
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
