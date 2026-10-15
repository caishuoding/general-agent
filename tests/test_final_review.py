"""通过公开聊天边界复现最终审查发现的资料范围与普通联网主题问题。"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_attachments import configure
from test_search import reply
from test_memory import configure_memory
from test_memory_recall import recall_provider
from test_streaming import wait_message


@pytest.mark.parametrize('selected', [True, False])
def test_question_uses_current_or_named_file_even_when_old_file_has_many_chunks(database_url: str, tmp_path: Path, selected: bool) -> None:
    """旧文件大量片段不能挤掉当前选择或具名文件，回答和出处均必须来自本次指定资料。"""
    def provider(request: httpx.Request) -> httpx.Response:
        """按实际收到的文件事实回答，协议同时支持同步与流式。"""
        payload=json.loads(request.content)
        sources=[]
        for message in payload['messages']:
            if isinstance(message['content'],str) and message['content'].startswith('以下 JSON 是当前会话附件'):
                sources=json.loads(message['content'].split('\n',1)[1])
        fresh=next((source for source in sources if 'NEW_CODE=GREEN' in source['excerpt']),None)
        source=fresh or (sources[0] if sources else None)
        return reply(payload,('GREEN' if fresh else 'RED')+(f" [{source['key']}]" if source else ''))

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        old=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('old.txt',b'OLD_CODE=RED.\n'*2000)}).json()[0]
        client.post(f'/api/conversations/{conversation}/messages',json={'content':'先收下旧文件','attachment_ids':[old['id']]})
        fresh=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('latest.txt',b'NEW_CODE=GREEN.')}).json()[0]
        if not selected:
            client.post(f'/api/conversations/{conversation}/messages',json={'content':'先收下新文件','attachment_ids':[fresh['id']]})
        body={'content':'本次所选文件的代号是什么？' if selected else 'latest.txt 的代号是什么？','attachment_ids':[fresh['id']] if selected else []}
        if selected:
            response=client.post(f'/api/conversations/{conversation}/messages',json=body)
            assert response.status_code==200
            answer=response.json()['messages'][-1]
        else:
            assert client.post(f'/api/conversations/{conversation}/runs',json=body).status_code==202
            answer=wait_message(client,conversation,'complete')
        assert 'GREEN' in answer['content']
        assert answer['citations'] and {source['attachment_id'] for source in answer['citations']}=={fresh['id']}


@pytest.mark.parametrize('with_attachment', [False, True])
def test_generic_web_topics_do_not_require_or_send_personal_attachments(database_url: str, tmp_path: Path, with_attachment: bool) -> None:
    """公开资料、图片和扩展名只是搜索主题，有无会话文件都不应自动读取附件。"""
    queries=[]
    planned=[]

    def provider(request: httpx.Request) -> httpx.Response:
        """外部搜索返回公开结果，并记录实际查询及任何不应出现的个人资料规划。"""
        payload=json.loads(request.content)
        if request.url.host=='search.example':
            queries.append(payload['query'])
            return httpx.Response(200,json={'results':[{'title':'公开资料','url':'https://public.example/info','content':'公开结果'}],'usage':{'credits':1}})
        if payload['messages'][0]['content'].startswith('为明确联网请求生成查询'):
            planned.append(payload)
            return reply(payload,json.dumps({'query':'不应规划个人资料'},ensure_ascii=False))
        return reply(payload,'公开查询回答。')

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation=configure(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test-key'})
        if with_attachment:
            file=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('private.txt',b'PERSONAL-ATTACHMENT-SECRET')}).json()[0]
            client.post(f'/api/conversations/{conversation}/messages',json={'content':'先收下','attachment_ids':[file['id']]})
        topics=['北京旅游资料','公开图片素材','PDF文件规范','PDF文件扩展名 .pdf','https://public.example/guide.pdf 的发布信息','Python参考资料','参考资料的格式']
        for topic in topics:
            response=client.post(f'/api/conversations/{conversation}/messages',json={'content':'联网搜索：'+topic})
            assert response.status_code==200, response.text
            answer=response.json()['messages'][-1]
            assert answer['search_action']['status']=='complete' and answer['search_action']['sources']==[]
        assert queries==topics and planned==[]


@pytest.mark.parametrize('with_memory', [False, True])
def test_public_memory_topics_do_not_read_personal_memories(database_url: str, tmp_path: Path, with_memory: bool) -> None:
    """人类记忆与记住知识的研究是公开主题，无论是否有正式记忆都不召回个人事实。"""
    queries=[]
    planned=[]
    embeddings=[]

    def provider(request: httpx.Request) -> httpx.Response:
        """记录真正外发的搜索与嵌入，只有准备阶段的显式保存允许使用受控嵌入。"""
        payload=json.loads(request.content)
        if request.url.path.endswith('/embeddings'):
            embeddings.append(payload)
            return recall_provider(request)
        if request.url.host=='search.example':
            queries.append(payload['query'])
            return httpx.Response(200,json={'results':[],'usage':{'credits':1}})
        if payload['messages'][0]['content'].startswith('为明确联网请求生成查询'):
            planned.append(payload)
            return reply(payload,json.dumps({'query':'不应规划个人记忆'},ensure_ascii=False))
        return reply(payload,'公开主题回答。')

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        old=configure_memory(client)
        client.put('/api/settings/search',json={'base_url':'https://search.example','api_key':'test-key'})
        if with_memory:
            assert client.post(f'/api/conversations/{old}/messages',json={'content':'请记住：我住在私人地址银河路123号'}).status_code==200
        conversation=client.post('/api/conversations',json={}).json()['id']
        before=len(embeddings)
        topics=['人类记忆研究','容易记住的英语单词教学方法','使用记忆宫殿提高学习效率','基于长期记忆机制的学习论文','我的记忆力为什么变差']
        for topic in topics:
            response=client.post(f'/api/conversations/{conversation}/messages',json={'content':'联网搜索：'+topic})
            assert response.status_code==200, response.text
            action=response.json()['messages'][-1]['search_action']
            assert action['status']=='complete' and action['sources']==[]
        assert queries==topics and planned==[] and len(embeddings)==before
