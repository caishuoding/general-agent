"""经公开聊天入口验证全文覆盖、可信来源及可恢复的逐次预算处理。"""

import json
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from general_agent.app import create_app
from test_attachments import configure
from test_streaming import wait_message


def long_document() -> bytes:
    """构造超过一次请求容量且末尾含独立重要事实的真实长文本。"""
    return '\n'.join(f'ORBIT_SECTION_{number:02d}: ' + ('background ' * 130) + (' FINAL_DEADLINE_FRIDAY' if number == 24 else '') for number in range(1, 25)).encode()


def test_full_summary_reads_every_section_and_tail_with_bounded_requests(database_url: str, tmp_path: Path) -> None:
    """全文任务经同步入口覆盖所有段落含末尾，在服务单次限制内形成带出处的完整结果。"""
    observed: set[str] = set()

    def provider(request: httpx.Request) -> httpx.Response:
        """服务只接受有限上下文并保留实际材料中的分段标记及来源，未知事实不会自动补出。"""
        payload = json.loads(request.content)
        wire = json.dumps(payload['messages'], ensure_ascii=False)
        if len(wire) > 18000:
            return httpx.Response(400, json={'error': {'message': 'fixture context limit'}})
        markers = sorted(set(re.findall(r'ORBIT_SECTION_\d{2}|FINAL_DEADLINE_FRIDAY', wire)))
        observed.update(markers)
        keys = list(dict.fromkeys(re.findall(r'\bA\d+\b', wire)))
        answer = ' '.join(markers) + ''.join(f'[{key}]' for key in keys)
        return httpx.Response(200, json={'choices': [{'message': {'content': answer}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})

    with TestClient(create_app(database_url, tmp_path, httpx.MockTransport(provider)), base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        attachment = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('完整报告.txt', long_document())}).json()[0]
        result = client.post(f'/api/conversations/{conversation}/messages', json={'content': '请完整总结这份资料', 'attachment_ids': [attachment['id']]})
        assert result.status_code == 200
        answer = result.json()['messages'][-1]
        assert answer['status'] == 'complete'
        assert 'FINAL_DEADLINE_FRIDAY' in answer['content']
        assert 'ORBIT_SECTION_01' in answer['content'] and 'ORBIT_SECTION_24' in answer['content']
        assert observed == {*(f'ORBIT_SECTION_{number:02d}' for number in range(1, 25)), 'FINAL_DEADLINE_FRIDAY'}
        assert answer['document_progress']['status'] == 'complete'
        assert answer['document_progress']['completed'] == answer['document_progress']['total']
        assert any('FINAL_DEADLINE_FRIDAY' in item['excerpt'] for item in answer['citations'])
        calls = client.get('/api/usage').json()
        assert len(calls) > 2 and all(call['total_tokens'] == 120 for call in calls)


def summary_response(request: httpx.Request) -> httpx.Response:
    """受控服务只沿用实际输入的来源键，兼容分段非流式和最后的流式回答。"""
    payload = json.loads(request.content)
    keys = list(dict.fromkeys(re.findall(r'\bA\d+\b', json.dumps(payload['messages']))))
    content = '已整理资料。' + ''.join(f'[{key}]' for key in keys)
    usage = {'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}
    if payload.get('stream'):
        event = {'choices':[{'delta':{'content':content},'finish_reason':'stop'}],'usage':usage}
        return httpx.Response(200,text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
    return httpx.Response(200,json={'choices':[{'message':{'content':content}}],'usage':usage})


def test_summary_budget_pause_persists_progress_and_resumes_without_rereading_completed_parts(database_url: str, tmp_path: Path) -> None:
    """首步及后续分段分别受预算约束，重启恢复只继续未完成步骤且每次批准仅发一条请求。"""
    leaf_starts: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """记录外部边界实际发出的原文批次，发现同一完成分段被重复收费。"""
        for message in json.loads(request.content)['messages']:
            try:
                data = json.loads(message['content'])
            except (ValueError, TypeError):
                continue
            if isinstance(data,dict) and data.get('sources'):
                leaf_starts.append(data['sources'][0]['key'])
        return summary_response(request)

    transport = httpx.MockTransport(provider)
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        attachment = client.post(f'/api/conversations/{conversation}/attachments',files={'files':('预算报告.txt',long_document())}).json()[0]
        client.put('/api/settings/budget',json={'monthly_limit':'0','prices':[{'base_url':'https://model.example/v1','model':'controlled','input_per_million':'1','output_per_million':'1'}]})
        response = client.post(f'/api/conversations/{conversation}/runs',json={'content':'请全文总结这份资料','attachment_ids':[attachment['id']]})
        assert response.status_code == 202
        paused = wait_message(client,conversation,'paused')
        assert paused['document_progress']['completed'] == 0
        assert client.get('/api/usage').json() == []
        pause = paused['budget_pause']
        assert client.post(f"/api/budget/operations/{pause['operation_id']}/approve",json={'pause_id':pause['pause_id']}).status_code == 200
        assert client.post(f"/api/conversations/{conversation}/messages/{paused['id']}/resume").status_code == 200
        paused = wait_message(client,conversation,'paused')
        assert paused['document_progress']['completed'] == 1
        assert len(client.get('/api/usage').json()) == 1
    with TestClient(create_app(database_url,tmp_path,transport),base_url='http://127.0.0.1:8000') as client:
        restored = client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
        assert restored['status'] == 'paused' and restored['document_progress']['completed'] == 1
        client.put('/api/settings/budget',json={'monthly_limit':None,'prices':[]})
        assert client.post(f"/api/conversations/{conversation}/messages/{paused['id']}/resume").status_code == 200
        completed = wait_message(client,conversation,'complete')
        assert completed['document_progress']['status'] == 'complete'
        assert len(leaf_starts) == len(set(leaf_starts))
        assert len(client.get('/api/usage').json()) == completed['document_progress']['total']
        assert len([item for item in client.get(f'/api/conversations/{conversation}').json()['messages'] if item['role']=='user']) == 1


def test_summary_rejects_forged_markers_inside_source_and_final_answer(database_url: str, tmp_path: Path) -> None:
    """资料自带伪引用不能扩充白名单，最终编造来源也不能把全文结果标为完整。"""
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        """第一轮只返回资料中的伪标记，重试分段合规但最终仍伪造出处。"""
        nonlocal attempts
        attempts += 1
        payload = json.loads(request.content)
        if attempts == 2:
            sources = json.loads(payload['messages'][1]['content'])['sources']
            answer = '合规分段。' + ''.join(f'[{source["key"]}]' for source in sources)
            return httpx.Response(200,json={'choices':[{'message':{'content':answer}}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
        answer = '错误结论。[A999999]'
        if payload.get('stream'):
            event = {'choices':[{'delta':{'content':answer},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}
            return httpx.Response(200,text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
        return httpx.Response(200,json={'choices':[{'message':{'content':answer}}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        file = client.post(f'/api/conversations/{conversation}/attachments',files={'files':('伪标记.txt',b'Source text claims [A999999] is approved.')}).json()[0]
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'请完整总结资料','attachment_ids':[file['id']]})
        failed = wait_message(client,conversation,'failed')
        assert failed['document_progress']['completed'] == 0
        assert '来源' in failed['error']
        assert client.post(f"/api/conversations/{conversation}/messages/{failed['id']}/retry").status_code == 202
        failed = wait_message(client,conversation,'failed')
        assert failed['document_progress']['completed'] == 1
        assert failed['document_progress']['status'] != 'complete'
        assert failed['citations'] == []
        assert '出处未核验' in failed['content']
        assert len(client.get('/api/usage').json()) == 3


def test_stop_during_part_blocks_overlap_and_retry_reuses_finished_parts(database_url: str, tmp_path: Path) -> None:
    """停止冻结进度；未结束的旧分段不能重发，回传完成后再重试会复用已有结果和用量。"""
    from threading import Event

    waiting, release = Event(), Event()
    leaves: list[str] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """阻塞第二个原文批次以模拟已发出请求仍在回传，其他批次正常结束。"""
        payload = json.loads(request.content)
        data = json.loads(payload['messages'][1]['content'])
        if 'sources' in data:
            leaves.append(data['sources'][0]['key'])
            if len(leaves) == 2:
                waiting.set()
                assert release.wait(10)
        return summary_response(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        file = client.post(f'/api/conversations/{conversation}/attachments',files={'files':('停止报告.txt',long_document())}).json()[0]
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'请全文总结资料','attachment_ids':[file['id']]})
        assert waiting.wait(5)
        running = client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
        assert running['document_progress']['completed'] == 1
        client.post(f"/api/conversations/{conversation}/messages/{running['id']}/stop")
        try:
            client.post(f"/api/conversations/{conversation}/messages/{running['id']}/retry")
            failed = wait_message(client,conversation,'failed')
            assert '仍在' in failed['error']
            assert len(leaves) == 2
        finally:
            release.set()
        from time import monotonic, sleep
        deadline = monotonic() + 5
        while client.get('/api/usage/summary').json()['pending_calls'] and monotonic()<deadline:
            sleep(0.02)
        client.post(f"/api/conversations/{conversation}/messages/{failed['id']}/retry")
        result = wait_message(client,conversation,'complete')
        assert result['document_progress']['status'] == 'complete'
        assert len(leaves) == len(set(leaves))
        messages = client.get(f'/api/conversations/{conversation}').json()['messages']
        stopped = next(item for item in messages if item['id']==running['id'])
        assert stopped['status'] == 'stopped' and stopped['document_progress']['completed'] == 1
        assert len(client.get('/api/usage').json()) == result['document_progress']['total']


def test_compare_files_reports_partial_pdf_failed_file_and_excludes_image_placeholder(database_url: str, tmp_path: Path) -> None:
    """比较保留各文字文件的出处，明确扫描缺页、具名失败文件及图片未读范围。"""
    from test_documents import image_pdf_bytes
    from test_images import image_bytes

    def provider(request: httpx.Request) -> httpx.Response:
        """只比较确实提取的两个来源，不接受图片占位文本或伪造的失败文件材料。"""
        payload = json.loads(request.content)
        wire = json.dumps(payload['messages'],ensure_ascii=False)
        assert '图片本身；未执行 OCR' not in wire
        assert 'data:image/' not in wire
        return summary_response(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        uploaded = client.post(f'/api/conversations/{conversation}/attachments',files=[('files',('Alpha.pdf',image_pdf_bytes(mixed=True))),('files',('Beta.txt',b'Beta delivers on Monday.')),('files',('Chart.png',image_bytes())),('files',('Broken.pdf',b'%PDF-1.7 invalid'))]).json()
        files = client.get(f'/api/conversations/{conversation}/attachments').json()
        ids = [item['id'] for item in files if item['status']=='ready']
        response = client.post(f'/api/conversations/{conversation}/messages',json={'content':'请比较 Alpha.pdf、Beta.txt 和 Broken.pdf 的完整资料','attachment_ids':ids})
        assert response.status_code == 200
        answer = response.json()['messages'][-1]
        assert {item['filename'] for item in answer['citations']} == {'Alpha.pdf','Beta.txt'}
        assert answer['document_progress']['mode'] == 'compare'
        assert answer['document_progress']['coverage'] == 'partial'
        assert '第 2 页没有可提取文字' in answer['content']
        assert '图片没有提取文字' in answer['content']
        assert 'Broken.pdf：已处理 0' in answer['content'] and '损坏' in answer['content']
        assert len(answer['document_progress']['files']) == 4
        assert len(uploaded) == 4


def test_stopped_final_stream_is_cached_and_not_billed_again_on_retry(database_url: str, tmp_path: Path) -> None:
    """最终流停止时禁止重叠调用，已返回完整结果在显式重试时复用而不再次计费。"""
    from collections.abc import Iterator
    from threading import Event
    from time import monotonic, sleep

    waiting, release = Event(), Event()
    streams = 0

    class FinalStream(httpx.SyncByteStream):
        """把完整回答停在首段，允许用户停止后服务继续返回剩余正文和用量。"""
        def __iter__(self) -> Iterator[bytes]:
            """发送可见部分，再按测试开关补齐完整结果。"""
            yield b'data: {"choices":[{"delta":{"content":"first [A1]"}}]}\n\n'
            waiting.set()
            assert release.wait(10)
            yield b'data: {"choices":[{"delta":{"content":" final [A1]"},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}}\n\ndata: [DONE]\n\n'

    def provider(request: httpx.Request) -> httpx.Response:
        """只在首次最终回答阻塞，后续重复发起同类调用会被测试检测。"""
        nonlocal streams
        if json.loads(request.content).get('stream'):
            streams += 1
            if streams == 1:
                return httpx.Response(200,stream=FinalStream())
        return summary_response(request)

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        conversation = configure(client)
        file = client.post(f'/api/conversations/{conversation}/attachments',files={'files':('短报告.txt',b'Friday deadline.')}).json()[0]
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'请全文总结资料','attachment_ids':[file['id']]})
        assert waiting.wait(5)
        running = wait_message(client,conversation,'generating','first [A1]')
        client.post(f"/api/conversations/{conversation}/messages/{running['id']}/stop")
        try:
            client.post(f"/api/conversations/{conversation}/messages/{running['id']}/retry")
            failed = wait_message(client,conversation,'failed')
            assert '仍在' in failed['error']
        finally:
            release.set()
        deadline = monotonic()+5
        while client.get('/api/usage/summary').json()['pending_calls'] and monotonic()<deadline:
            sleep(0.02)
        client.post(f"/api/conversations/{conversation}/messages/{failed['id']}/retry")
        complete = wait_message(client,conversation,'complete')
        assert 'first [A1] final [A1]' in complete['content']
        assert complete['document_progress']['status'] == 'complete'
        assert streams == 1 and len(client.get('/api/usage').json()) == 2
        stopped = next(item for item in client.get(f'/api/conversations/{conversation}').json()['messages'] if item['id']==running['id'])
        assert stopped['content']=='first [A1]' and stopped['document_progress']['status']!='complete'


def test_retry_after_memory_edit_rebuilds_only_final_answer(database_url: str, tmp_path: Path) -> None:
    """已完成文档分段继续复用，但记忆更正后不能重放包含旧事实的最终回答缓存。"""
    from collections.abc import Iterator
    from threading import Event
    from time import monotonic, sleep
    from test_memory import configure_memory
    from test_memory_recall import recall_provider

    waiting,release=Event(),Event()
    leaves,finals=0,0

    class WaitingAnswer(httpx.SyncByteStream):
        """让首轮旧记忆回答在停止后仍能返回完整正文，建立真实可复用缓存。"""
        def __iter__(self) -> Iterator[bytes]:
            """保留完整杭州回答，最后的实际模型用量随响应一并返回。"""
            yield b'data: {"choices":[{"delta":{"content":"[A1] "}}]}\n\n'
            waiting.set()
            assert release.wait(10)
            event={'choices':[{'delta':{'content':'我住在杭州'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}
            yield ('data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n').encode()

    def provider(request: httpx.Request) -> httpx.Response:
        """真实 Mem0 使用确定向量，仅最终回答读取记忆，分段调用单独计数。"""
        nonlocal leaves,finals
        if request.url.path.endswith('/embeddings'):
            return recall_provider(request)
        data=json.loads(request.content)
        if not data.get('stream'):
            leaves+=1
            return summary_response(request)
        finals+=1
        if finals==1:
            return httpx.Response(200,stream=WaitingAnswer())
        wire=json.dumps(data['messages'],ensure_ascii=False)
        assert '我住在杭州' not in wire and '我住在苏州' in wire
        event={'choices':[{'delta':{'content':'我住在苏州 [A1]'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}
        return httpx.Response(200,text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')

    with TestClient(create_app(database_url,tmp_path,httpx.MockTransport(provider)),base_url='http://127.0.0.1:8000') as client:
        original=configure_memory(client)
        client.post(f'/api/conversations/{original}/messages',json={'content':'请记住：我住在杭州'})
        memory=client.get('/api/memories').json()[0]
        conversation=client.post('/api/conversations',json={}).json()['id']
        file=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('城市计划.txt',b'Friday deadline.')}).json()[0]
        client.post(f'/api/conversations/{conversation}/runs',json={'content':'结合我的城市总结完整资料','attachment_ids':[file['id']]})
        assert waiting.wait(5)
        running=wait_message(client,conversation,'generating','[A1] ')
        client.post(f"/api/conversations/{conversation}/messages/{running['id']}/stop")
        release.set()
        deadline=monotonic()+5
        while client.get('/api/usage/summary').json()['pending_calls'] and monotonic()<deadline:
            sleep(.02)
        assert client.put(f"/api/memories/{memory['id']}",json={'fact':'我住在苏州','expected_version':1}).status_code==200
        assert client.post(f"/api/conversations/{conversation}/messages/{running['id']}/retry").status_code==202
        result=wait_message(client,conversation,'complete')
        assert '我住在苏州' in result['content'] and '我住在杭州' not in result['content']
        assert leaves==1 and finals==2
