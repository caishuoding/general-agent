"""通过外部检查点适配器故障注入覆盖提交窗口及长时间离线续接。"""

import json
import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import httpx
import psycopg
import pytest

from tests.auth.support import login_admin_http, switch_to_normal_user


@pytest.mark.parametrize('window', ['before_input', 'after_finalize', 'before_finalize', 'offline'])
def test_recovery_handles_commit_windows_and_offline_time(database_url: str, tmp_path: Path, window: str) -> None:
    """首次检查点前或收尾提交后崩溃不丢失上下文，离线超过期限仍保留剩余时长。"""
    observed: list[dict[str, Any]] = []
    waiting, release = Event(), Event()
    marker = tmp_path / 'fault-reached'
    hook = '''"""只在测试进程的外部 LangGraph saver 边界暂停一次提交。"""
import os
from dataclasses import replace
from pathlib import Path
from threading import Event
from langgraph.checkpoint.postgres import PostgresSaver
import general_agent.infrastructure.agent_limits as limits
mode = os.environ['TEST_RECOVERY_WINDOW']
if mode == 'offline':
    limits.LIMITS = replace(limits.LIMITS, answer_seconds=3, run_seconds=3)
marker = Path(os.environ['TEST_RECOVERY_MARKER'])
original = PostgresSaver.put
positions = {}
def put(self, config, checkpoint, metadata, new_versions):
    """在真实数据库提交前暂停，业务结果仍通过正常应用接口产生。"""
    values = checkpoint.get('channel_values', {})
    start = values.get('__start__', {})
    if isinstance(start, dict) and start.get('answer_id'):
        positions[start['answer_id']] = start['history_position']
    before = mode == 'before_input' and isinstance(start, dict) and any(item.get('content') == '第二轮' for item in start.get('messages', []))
    after = mode == 'after_finalize' and values.get('answer') and values.get('history_position', 0) > positions.get(values.get('answer_id'), float('inf'))
    if (before or after) and not marker.exists():
        marker.write_text('reached')
        Event().wait(30)
    return original(self, config, checkpoint, metadata, new_versions)
PostgresSaver.put = put
if mode == 'before_finalize':
    from general_agent.agent.conversation import ConversationGraph
    original_finalize = ConversationGraph.finalize
    def finalize(self, state, runtime):
        """在 Agent 已完成、业务 finalize 尚未提交时暂停一次。"""
        if not marker.exists():
            marker.write_text('reached')
            Event().wait(30)
        return original_finalize(self, state, runtime)
    ConversationGraph.finalize = finalize
'''
    (tmp_path / 'recovery_hook.py').write_text(hook)

    class Provider(BaseHTTPRequestHandler):
        """向真实应用返回带终止帧的 SSE，离线场景阻塞首次回答。"""

        def do_POST(self) -> None:
            """记录实际上下文并在恢复后返回可核对的唯一最终文本。"""
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            observed.append(payload)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if window == 'offline' and len(observed) == 1:
                waiting.set()
                release.wait(30)
                return
            text = '第一轮唯一回答' if len(observed) == 1 else '后续回答'
            self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode())
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            """测试不输出认证或模型输入。"""
            return

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    Thread(target=provider.serve_forever, daemon=True).start()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    environment = {**os.environ, 'DATABASE_URL': database_url, 'APP_DATA_DIR': str(tmp_path),
        'PYTHONPATH': str(tmp_path) + os.pathsep + str(Path(__file__).resolve().parents[2] / 'backend'),
        'TEST_RECOVERY_WINDOW': window, 'TEST_RECOVERY_MARKER': str(marker)}

    def launch() -> subprocess.Popen[bytes]:
        """以同一临时数据目录重启应用，故障仅触发一次。"""
        with (tmp_path / 'app.log').open('ab') as log:
            return subprocess.Popen([sys.executable, '-c',
                f'import recovery_hook; import uvicorn; uvicorn.run("general_agent.app:create_app", factory=True, host="127.0.0.1", port={port}, log_level="error")'], env=environment, stdout=log, stderr=log)

    def ready(client: httpx.Client) -> None:
        """等待真实后端启动，错误带上临时日志。"""
        deadline = monotonic() + 15
        while monotonic() < deadline:
            try:
                if client.get('/api/health').status_code == 200:
                    return
            except httpx.RequestError:
                pass
            sleep(0.05)
        raise AssertionError((tmp_path / 'app.log').read_text())

    def messages(client: httpx.Client, conversation: str, status: str) -> list[dict[str, Any]]:
        """只通过公开会话接口等待目标终态。"""
        deadline = monotonic() + 10
        while monotonic() < deadline:
            rows = client.get(f'/api/conversations/{conversation}').json()['messages']
            if rows and rows[-1]['status'] == status:
                return list(rows)
            sleep(0.05)
        raise AssertionError(rows)

    process = launch()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=5) as client:
            ready(client)
            login_admin_http(client)
            assert client.put('/api/settings', json={'base_url': f'http://127.0.0.1:{provider.server_port}/v1',
                'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144}).status_code == 200
            switch_to_normal_user(client)
            conversation = client.post('/api/conversations', json={}).json()['id']
            assert client.post(f'/api/conversations/{conversation}/runs', json={'content': '第一轮'}).status_code == 202
            if window == 'before_input':
                messages(client, conversation, 'complete')
                assert client.post(f'/api/conversations/{conversation}/runs', json={'content': '第二轮'}).status_code == 202
            if window == 'offline':
                assert waiting.wait(5)
            else:
                deadline = monotonic() + 10
                while not marker.exists() and monotonic() < deadline:
                    sleep(0.02)
                assert marker.exists()
            process.kill()
            process.wait(timeout=5)
            if window == 'offline':
                sleep(4)
            process = launch()
            ready(client)
            if window == 'after_finalize':
                rows = messages(client, conversation, 'complete')
                assert client.post(f'/api/conversations/{conversation}/runs', json={'content': '第二轮'}).status_code == 202
            else:
                rows = messages(client, conversation, 'interrupted')
                resumed = client.post(f"/api/conversations/{conversation}/messages/{rows[-1]['id']}/resume")
                assert resumed.status_code == 202, resumed.text
            final = messages(client, conversation, 'complete')
            if window in ('before_input', 'after_finalize'):
                assert [item.get('content') for item in observed[-1]['messages']].count('第一轮唯一回答') == 1
                assert len(final) == 4
            else:
                assert len(final) == 2
                assert final[-1]['content'] in {'第一轮唯一回答', '后续回答'}
            with psycopg.connect(database_url) as connection:
                closed = connection.execute(
                    "SELECT closed FROM agents WHERE answer_attempt_id=%s AND parent_agent_id IS NULL",
                    (final[-1]['id'],),
                ).fetchone()
            assert closed == (True,)
    finally:
        process.terminate()
        process.wait(timeout=10)
        release.set()
        provider.shutdown()
        provider.server_close()
