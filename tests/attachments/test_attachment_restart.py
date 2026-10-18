"""通过真实后端进程终止验证处理中附件不会永久假装可用。"""

import os
import socket
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

import httpx


def test_killed_parser_reports_failed_after_restart(database_url: str, tmp_path: Path) -> None:
    """上传完成而解析中进程被终止时保留原文件，重启给出失败状态并拒绝当作可读资料。"""
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    environment = {**os.environ, 'DATABASE_URL': database_url, 'APP_DATA_DIR': str(tmp_path), 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'backend')}

    def launch() -> subprocess.Popen[bytes]:
        """只在隔离测试数据库和临时目录启动一次本机后端。"""
        return subprocess.Popen([sys.executable, '-m', 'uvicorn', 'general_agent.app:create_app', '--factory', '--host', '127.0.0.1', '--port', str(port), '--log-level', 'error'], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def ready(client: httpx.Client) -> None:
        """等待公开健康接口就绪，不读取进程内部状态。"""
        deadline = monotonic() + 10
        while monotonic() < deadline:
            try:
                if client.get('/api/health').status_code == 200:
                    return
            except httpx.RequestError:
                pass
            sleep(0.05)
        raise AssertionError('测试后端未就绪')

    process = launch()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=15) as client:
            ready(client)
            client.put('/api/settings', json={'base_url': 'https://unused.example/v1', 'model': 'unused', 'api_key': 'unused'})
            conversation = client.post('/api/conversations', json={}).json()['id']
            response = client.post(f'/api/conversations/{conversation}/attachments', files={'files': ('long.txt', b'a' * 20_000_000)})
            assert response.status_code == 201
            attachment = response.json()[0]
            assert attachment['status'] == 'processing'
            process.kill()
            process.wait(timeout=5)
            process = launch()
            ready(client)
            restored = client.get(f'/api/conversations/{conversation}/attachments').json()[0]
            assert restored['status'] == 'failed'
            assert '重启中断' in restored['error']
            assert len(client.get(f'/api/conversations/{conversation}/attachments/{attachment["id"]}/file').content) == 20_000_000
            rejected = client.post(f'/api/conversations/{conversation}/runs', json={'content': '请读资料', 'attachment_ids': [attachment['id']]})
            assert rejected.status_code == 409
            assert client.get('/api/usage').json() == []
    finally:
        process.terminate()
        process.wait(timeout=5)
