"""通过真实后端进程终止验证全文检查点与未确认调用的区别。"""

import json
import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import httpx

from test_summary import long_document, summary_response


def test_killed_summary_reuses_completed_parts_and_retries_only_interrupted_part(database_url: str, tmp_path: Path) -> None:
    """分段中强杀进程后保留完整步骤及其他会话的预算暂停，重试只重做未获结果的一段。"""
    blocked, release = Event(), Event()
    starts: list[str] = []

    class Provider(BaseHTTPRequestHandler):
        """真实本机HTTP替身，第二条原文请求在后端被终止前不返回。"""
        def do_POST(self) -> None:
            """记录原文批次并以标准JSON/SSE返回，连接被终止时不伪报应用已收到。"""
            raw = self.rfile.read(int(self.headers.get('Content-Length','0')))
            payload = json.loads(raw)
            data = json.loads(payload['messages'][1]['content'])
            if 'sources' in data:
                starts.append(data['sources'][0]['key'])
                if len(starts)==2:
                    blocked.set()
                    release.wait(15)
            response = summary_response(httpx.Request('POST','http://controlled/v1/chat/completions',content=raw))
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream' if payload.get('stream') else 'application/json')
            self.send_header('Content-Length',str(len(response.content)))
            self.end_headers()
            try:
                self.wfile.write(response.content)
            except (BrokenPipeError,ConnectionResetError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            """测试替身不打印文件正文或请求凭据。"""
            return

    provider = ThreadingHTTPServer(('127.0.0.1',0),Provider)
    Thread(target=provider.serve_forever,daemon=True).start()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1',0))
        port = reserved.getsockname()[1]
    environment = {**os.environ,'DATABASE_URL':database_url,'APP_DATA_DIR':str(tmp_path),'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'backend')}

    def launch() -> subprocess.Popen[bytes]:
        """仅使用本测试的数据库和临时配置启动独立后端。"""
        return subprocess.Popen([sys.executable,'-m','uvicorn','general_agent.app:create_app','--factory','--host','127.0.0.1','--port',str(port),'--log-level','error'],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    def ready(client: httpx.Client) -> None:
        """等待健康接口就绪，连接拒绝只属于启动过程而非业务请求重试。"""
        deadline=monotonic()+10
        while monotonic()<deadline:
            try:
                if client.get('/api/health').status_code==200:
                    return
            except httpx.RequestError:
                pass
            sleep(.05)
        raise AssertionError('隔离服务未启动')

    process=launch()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=5,trust_env=False) as client:
            ready(client)
            client.put('/api/settings',json={'base_url':f'http://127.0.0.1:{provider.server_port}/v1','model':'controlled','api_key':'test-key'})
            paused_conversation=client.post('/api/conversations',json={}).json()['id']
            paused_file=client.post(f'/api/conversations/{paused_conversation}/attachments',files={'files':('paused.txt',b'Paused independent document.')}).json()[0]
            deadline=monotonic()+5
            while client.get(f'/api/conversations/{paused_conversation}/attachments').json()[0]['status']=='processing' and monotonic()<deadline:
                sleep(.02)
            client.put('/api/settings/budget',json={'monthly_limit':'0','prices':[]})
            client.post(f'/api/conversations/{paused_conversation}/runs',json={'content':'请全文总结资料','attachment_ids':[paused_file['id']]})
            deadline=monotonic()+5
            while monotonic()<deadline:
                paused=client.get(f'/api/conversations/{paused_conversation}').json()['messages'][-1]
                if paused['status']=='paused':
                    break
                sleep(.02)
            assert paused['status']=='paused'
            client.put('/api/settings/budget',json={'monthly_limit':None,'prices':[]})
            conversation=client.post('/api/conversations',json={}).json()['id']
            uploaded=client.post(f'/api/conversations/{conversation}/attachments',files={'files':('restart.txt',long_document())}).json()[0]
            deadline=monotonic()+5
            while client.get(f'/api/conversations/{conversation}/attachments').json()[0]['status']=='processing' and monotonic()<deadline:
                sleep(.02)
            client.post(f'/api/conversations/{conversation}/runs',json={'content':'请全文总结资料','attachment_ids':[uploaded['id']]})
            assert blocked.wait(5)
            before=client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
            assert before['document_progress']['completed']==1
            process.kill()
            process.wait(timeout=5)
            release.set()
            process=launch()
            ready(client)
            restored=client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
            assert restored['status']=='failed' and restored['document_progress']['completed']==1
            assert '重启' in restored['error']
            still_paused=client.get(f'/api/conversations/{paused_conversation}').json()['messages'][-1]
            assert still_paused['status']=='paused'
            assert still_paused['budget_pause']==paused['budget_pause']
            assert still_paused['document_progress']==paused['document_progress']
            assert client.get('/api/usage/summary').json()['pending_calls']==0
            assert client.post(f"/api/conversations/{conversation}/messages/{restored['id']}/retry").status_code==202
            deadline=monotonic()+8
            while monotonic()<deadline:
                result=client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
                if result['status']!='generating':
                    break
                sleep(.02)
            assert result['status']=='complete'
            assert starts.count(starts[0])==1
            assert starts.count(starts[1])==2
            assert len(client.get('/api/usage').json())==result['document_progress']['total']+1
            assert sum(call['total_tokens'] is None for call in client.get('/api/usage').json())==1
            assert client.post(f"/api/conversations/{paused_conversation}/messages/{paused['id']}/resume").status_code==200
            deadline=monotonic()+5
            while monotonic()<deadline:
                resumed=client.get(f'/api/conversations/{paused_conversation}').json()['messages'][-1]
                if resumed['status']!='generating':
                    break
                sleep(.02)
            assert resumed['status']=='complete' and resumed['document_progress']['completed']==2
    finally:
        release.set()
        process.terminate()
        process.wait(timeout=5)
        provider.shutdown()
        provider.server_close()
