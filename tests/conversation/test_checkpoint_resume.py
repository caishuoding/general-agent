"""从公开聊天接口验证真实进程退出后的检查点续接。"""

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
import pytest

from tests.auth.support import login_admin_http, switch_to_normal_user


@pytest.mark.parametrize("mode", ["clock", "skill", "child"])
def test_restart_resumes_same_answer_after_completed_tool(database_url: str, tmp_path: Path, mode: str) -> None:
    """模型工具回合已完成后杀进程，显式续接复用原调用和用户消息。"""
    waiting, release, final_waiting, finish = Event(), Event(), Event(), Event()
    requests: list[dict[str, Any]] = []
    with_skill, with_child = mode == 'skill', mode == 'child'
    child_attempts = 0
    child_seen = Event()
    catalog = tmp_path / 'catalog' / 'demo'
    catalog.mkdir(parents=True)
    (catalog / 'SKILL.md').write_text('---\nname: demo\ndescription: 测试工作文件\nmetadata:\n  version: "1"\n---\n# 完整指令\n写入并读取实际文件。\n')

    class Provider(BaseHTTPRequestHandler):
        """在工具完成后的模型请求阻塞，恢复后返回最终回答。"""

        def do_POST(self) -> None:
            """提供真实 SSE；在模型、Skill 或子执行中断点等待进程重启。"""
            nonlocal child_attempts
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            requests.append(payload)
            is_child = str(payload['messages'][0]['content']).startswith('你是负责指定子任务')
            index = len(requests)
            if with_child:
                if is_child:
                    child_attempts += 1
                    if child_attempts == 1:
                        child_seen.set()
                        release.wait(45)
                        return
                    call = None
                    child_content = '子执行已恢复并完成核验。'
                else:
                    outputs = [item for item in payload['messages'] if item['role'] == 'tool']
                    if not outputs:
                        call = ('spawn_agent', {'title': '核验', 'task': '独立核验', 'expected_output': '核验结果'})
                    elif '子执行已恢复并完成核验' in json.dumps(payload['messages'], ensure_ascii=False):
                        call = None
                    else:
                        call = ('wait_agents', {})
                        assert child_seen.wait(5)
                        waiting.set()
            elif with_skill and index == 1:
                call = ('load_skill', {'name': 'demo'})
            elif with_skill and index == 2:
                loaded = json.loads(next(item['content'] for item in payload['messages'] if item['role'] == 'tool'))
                assert loaded['content'].startswith('---\nname: demo')
                assert 'task_id' not in loaded
                call = ('write_work_file', {'path': 'notes.txt', 'content': '持久工作内容'})
            elif index == (3 if with_skill else 2):
                waiting.set()
                release.wait(45)
                return
            elif with_skill and index == 4:
                assert 'read_work_file' in [tool['function']['name'] for tool in payload['tools']]
                assert '完整指令' in json.dumps(payload['messages'], ensure_ascii=False)
                call = ('read_work_file', {'path': 'notes.txt'})
            elif not with_skill and index == 1:
                call = ('get_current_time', {})
            else:
                call = None
            delta: dict[str, Any]
            if call:
                delta = {'tool_calls': [{'index': 0, 'id': 'call-' + str(index), 'type': 'function',
                    'function': {'name': call[0], 'arguments': json.dumps(call[1])}}]}
            else:
                if with_skill:
                    assert '持久工作内容' in payload['messages'][-1]['content']
                if with_child and is_child:
                    delta = {'content': child_content}
                else:
                    final_waiting.set()
                    finish.wait(10)
                    delta = {'content': '从已完成工具之后继续。'}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': 'tool_calls' if 'tool_calls' in delta else 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode())
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            """避免测试输出模型输入和登录信息。"""
            return

    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    Thread(target=provider.serve_forever, daemon=True).start()
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    environment = {**os.environ, 'DATABASE_URL': database_url, 'APP_DATA_DIR': str(tmp_path),
        'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'backend'), 'SKILLS_DIR': str(catalog.parent)}

    def launch() -> subprocess.Popen[bytes]:
        """启动隔离后端，日志落在临时目录便于排查恢复失败。"""
        with (tmp_path / 'backend.log').open('ab') as log:
            return subprocess.Popen([sys.executable, '-m', 'uvicorn', 'general_agent.app:create_app',
                '--factory', '--host', '127.0.0.1', '--port', str(port), '--log-level', 'error'],
                env=environment, stdout=log, stderr=log)

    def ready(client: httpx.Client) -> None:
        """等待健康接口，失败时带上隔离后端日志。"""
        deadline = monotonic() + 15
        while monotonic() < deadline:
            try:
                if client.get('/api/health').status_code == 200:
                    return
            except httpx.RequestError:
                pass
            sleep(0.05)
        raise AssertionError((tmp_path / 'backend.log').read_text())

    process = launch()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=5) as client:
            ready(client)
            login_admin_http(client)
            configured = client.put('/api/settings', json={'base_url': f'http://127.0.0.1:{provider.server_port}/v1',
                'model': 'controlled', 'api_key': 'test-key', 'context_window_tokens': 262144})
            assert configured.status_code == 200
            switch_to_normal_user(client)
            conversation = client.post('/api/conversations', json={}).json()['id']
            assert client.post(f'/api/conversations/{conversation}/runs', json={'content': '先读取时间再回答'}).status_code == 202
            assert waiting.wait(10), (client.get(f'/api/conversations/{conversation}').json()['messages'][-1].get('error'), len(requests))
            history = client.get(f'/api/conversations/{conversation}').json()['messages']
            answer = history[-1]['id']
            root_payload = next(payload for payload in reversed(requests) if not str(payload['messages'][0]['content']).startswith('你是负责指定子任务'))
            original_tools = [item for item in root_payload['messages'] if item['role'] == 'tool']
            before_restart = len(requests)
            process.kill()
            process.wait(timeout=5)
            process = launch()
            ready(client)
            restored = client.get(f'/api/conversations/{conversation}').json()['messages'][-1]
            assert restored['status'] == 'interrupted'
            assert len(requests) == before_restart, '重启不得自动向模型发请求'
            login_admin_http(client)
            switch_to_normal_user(client, username='other_account')
            assert client.post(f'/api/conversations/{conversation}/messages/{answer}/resume').status_code == 404
            logged = client.post('/api/auth/login', json={'username': 'test_user', 'password': 'test-user-password', 'expected_role': 'user'})
            assert logged.status_code == 200
            continued = client.post(f'/api/conversations/{conversation}/messages/{answer}/resume')
            assert continued.status_code == 202, continued.text
            assert final_waiting.wait(10)
            before_duplicate = len(requests)
            duplicate = client.post(f'/api/conversations/{conversation}/messages/{answer}/resume')
            assert duplicate.status_code == 202, duplicate.text
            assert len(requests) == before_duplicate
            finish.set()
            deadline = monotonic() + 10
            while monotonic() < deadline:
                result = client.get(f'/api/conversations/{conversation}').json()['messages']
                if result[-1]['status'] != 'generating':
                    break
                sleep(0.05)
            assert [(item['id'], item['role']) for item in result] == [(item['id'], item['role']) for item in history]
            assert result[-1]['status'] == 'complete', result[-1]
            assert result[-1]['content'] == '从已完成工具之后继续。'
            assert [item for item in requests[-1]['messages'] if item['role'] == 'tool'][:len(original_tools)] == original_tools
            assert len([item for item in requests[-1]['messages'] if item['role'] == 'user' and item['content'] == '先读取时间再回答']) == 1
            if with_child:
                assert len(result[-1]['agent_tasks']) == 1
                assert result[-1]['agent_tasks'][0]['status'] == 'completed'
            assert client.post(f'/api/conversations/{conversation}/messages/{answer}/resume').status_code == 422
    finally:
        process.terminate()
        process.wait(timeout=10)
        release.set()
        finish.set()
        provider.shutdown()
        provider.server_close()
