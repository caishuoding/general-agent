"""验证公开记忆主题不会误读取个人长期记忆。"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app
from tests.auth.support import authenticated_client
from tests.memory.test_memory import configure_memory
from tests.memory.test_memory_recall import recall_provider
from tests.search.test_search import last_user_content, reply, tool_call, tool_messages


@pytest.mark.parametrize('with_memory', [False, True])
def test_public_memory_topics_do_not_read_private_long_term_memory(database_url: str, tmp_path: Path, with_memory: bool) -> None:
    """人类记忆与记住知识的研究是公开主题，无论是否有正式记忆都不召回个人事实。"""
    queries: list[str] = []
    embeddings: list[dict[str, object]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """记录真正外发的搜索与嵌入，只有准备阶段的显式保存允许使用受控嵌入。"""
        payload = json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            embeddings.append(payload)
            return recall_provider(request)
        if request.url.host == 'search.example':
            queries.append(payload['search_query'])
            return httpx.Response(200, json={'search_result': []})
        messages = tool_messages(payload)
        if messages:
            return reply(payload, '公开主题回答。')
        if payload.get('tools'):
            content = last_user_content(payload)
            if content.startswith('请记住'):
                return tool_call('add_memory', {})
            if content.startswith('联网搜索：'):
                return tool_call('search_web', {'query': content.removeprefix('联网搜索：')})
            return reply(payload, '公开主题回答。')
        return reply(payload, '公开主题回答。')

    with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
        old = configure_memory(client)
        client.put('/api/settings/search', json={'base_url': 'https://search.example', 'api_key': 'test-key'})
        if with_memory:
            assert client.post(f'/api/conversations/{old}/messages', json={'content': '请记住：我住在私人地址银河路123号'}).status_code == 200
        conversation = client.post('/api/conversations', json={}).json()['id']
        before = len(embeddings)
        topics = ['人类记忆研究', '容易记住的英语单词教学方法', '使用记忆宫殿提高学习效率', '基于长期记忆机制的学习论文', '我的记忆力为什么变差']
        for topic in topics:
            response = client.post(f'/api/conversations/{conversation}/messages', json={'content': '联网搜索：' + topic})
            assert response.status_code == 200, response.text
            action = response.json()['messages'][-1]['search_action']
            assert action['status'] == 'complete'
        assert queries == topics and len(embeddings) == before
