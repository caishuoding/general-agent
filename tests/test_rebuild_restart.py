"""用真实后端进程强杀核验重建检查点，不重启共享数据库。"""

import json
import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4

import httpx


def test_killed_rebuild_reuses_committed_vectors_and_keeps_unknown_usage(database_url: str, tmp_path: Path) -> None:
    """进程在第二条嵌入途中终止后只重试该条，第一条目标向量及稳定身份保留。"""
    entered,release=Event(),Event()
    target_facts: list[str]=[]
    class Provider(BaseHTTPRequestHandler):
        """真实本机外部协议替身，第二条目标事实响应等待进程终止。"""
        def do_POST(self) -> None:
            """返回标准嵌入；被杀请求的响应不被视为应用已经确认的用量。"""
            payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
            if payload['model']=='new-embed' and payload['input']!=['连接验证']:
                target_facts.extend(payload['input'])
                if len(target_facts)==2:
                    entered.set()
                    release.wait(15)
            dimensions=4 if payload['model']=='new-embed' else 3
            response=json.dumps({'model':payload['model'],'data':[{'index':i,'embedding':[1.0]+[0.0]*(dimensions-1)} for i,_ in enumerate(payload['input'])],'usage':{'prompt_tokens':7,'total_tokens':7}}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(response)))
            self.end_headers()
            try:
                self.wfile.write(response)
            except (BrokenPipeError,ConnectionResetError):
                pass

        def log_message(self, format: str, *args: object) -> None:
            """不输出受控请求的正文及凭据。"""
            return

    provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    Thread(target=provider.serve_forever,daemon=True).start()
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0))
        port=reservation.getsockname()[1]
    environment={**os.environ,'DATABASE_URL':database_url,'APP_DATA_DIR':str(tmp_path),'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'backend')}
    def launch() -> subprocess.Popen[bytes]:
        """仅启动本测试的应用目录和随机库，不使用用户主服务或凭据。"""
        return subprocess.Popen([sys.executable,'-m','uvicorn','general_agent.app:create_app','--factory','--host','127.0.0.1','--port',str(port),'--log-level','error'],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def ready(client: httpx.Client) -> None:
        """等待实际进程完成迁移及恢复，启动期拒绝连接不视为业务重试。"""
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
            base=f'http://127.0.0.1:{provider.server_port}/v1'
            client.put('/api/settings',json={'base_url':base,'model':'chat','api_key':'test'})
            client.put('/api/settings/embedding',json={'base_url':base,'model':'embed','dimensions':3,'api_key':'test'})
            assert client.post('/api/settings/capabilities/embedding').json()['status']=='passed'
            cid=client.post('/api/conversations',json={}).json()['id']
            for fact in ['我喜欢茶','我住在杭州']:
                assert client.post(f'/api/conversations/{cid}/messages',json={'content':'请记住：'+fact}).status_code==200
            before=client.get('/api/memories').json()
            identifier=str(uuid4())
            client.post('/api/settings/embedding/rebuilds',json={'id':identifier,'target':{'base_url':base,'model':'new-embed','dimensions':4,'api_key':'test'},'confirmed':True})
            assert entered.wait(8)
            path=f'/api/settings/embedding/rebuilds/{identifier}'
            assert client.get(path).json()['completed']==1
            process.kill()
            process.wait(timeout=5)
            release.set()
            process=launch()
            ready(client)
            interrupted=client.get(path).json()
            assert interrupted['status']=='failed' and interrupted['completed']==1
            assert len(target_facts)==2
            assert client.get('/api/usage/summary').json()['pending_calls']==0
            assert client.post(path+'/resume').status_code==202
            deadline=monotonic()+10
            while monotonic()<deadline:
                result=client.get(path).json()
                if result['status']=='complete':
                    break
                sleep(.03)
            assert result['status']=='complete',result
            assert target_facts.count(target_facts[0])==1 and target_facts.count(target_facts[1])==2
            assert client.get('/api/memories').json()==before
            calls=client.get('/api/usage').json()
            assert sum(row['total_tokens'] is None for row in calls)==1
    finally:
        release.set()
        process.terminate()
        process.wait(timeout=5)
        provider.shutdown()
        provider.server_close()
