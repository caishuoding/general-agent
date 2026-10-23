"""为浏览器测试启动隔离数据库、临时配置和受控模型 HTTP 服务。"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep, time
from uuid import uuid4

import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from general_agent.app import create_app


class ControlledModel(BaseHTTPRequestHandler):
    """只在测试进程中运行的模型替身，不用于产品演示或真实验收。"""

    attempts: dict[str, int] = {}

    def do_POST(self) -> None:
        """支持当前 Files、文档视觉及按需读取协议，保留慢速和断流验收。"""
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == '/files':
            self.send_json({'id': 'file-api-document-browser', 'created_at': int(time()), 'expires_at': int(time()) + 2592000})
            return
        payload = json.loads(raw)
        if not payload.get('stream') and any(isinstance(item.get('content'), list) for item in payload['messages']):
            self.send_json({'choices': [{'message': {'content': json.dumps({'description': '受控扫描图片描述', 'ocr_text': '', 'caption': '', 'uncertainties': []})}}]})
            return
        prompt = next((message['content'] for message in reversed(payload['messages'])
                       if message['role'] == 'user' and isinstance(message.get('content'), str)), '')
        document_reply: str | None = None
        document_id: str | None = None
        for message in payload['messages']:
            content = message.get('content', '')
            if message['role'] == 'tool':
                result = json.loads(content)
                blocks = result.get('content', result.get('results', []))
                if isinstance(blocks, list):
                    document_reply = '\n'.join(str(item.get('content', '')) for item in blocks)
            if isinstance(content, str) and content.startswith('['):
                try:
                    attachments = json.loads(content)
                except ValueError:
                    attachments = None
                if isinstance(attachments, list) and attachments:
                    document_id = next((str(item['document_id']) for item in attachments if isinstance(item, dict) and item.get('document_id')), None)
        if payload.get('stream') and document_id and document_reply is None:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.write_event({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'document-read', 'type': 'function',
                'function': {'name': 'read_document', 'arguments': json.dumps({'document_id': document_id})}}]}, 'finish_reason': 'tool_calls'}]})
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            return
        if not payload.get("stream"):
            if payload.get("tools") and document_reply is None:
                content = "普通回答"
            else:
                content = document_reply or "这是受控模型的测试回答。"
            body = json.dumps({"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 12, "completion_tokens": 10, "total_tokens": 22}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.attempts[prompt] = self.attempts.get(prompt, 0) + 1
        slow = "分段" in prompt or "停止" in prompt or "断流" in prompt
        first = "第一段" if slow else "这是受控"
        second = "，第二段。" if slow else "模型的测试回答。"
        if document_reply is not None:
            first, second = document_reply, ""
        self.write_event({"choices": [{"delta": {"content": first}}]})
        sleep(2 if slow else 0.05)
        self.write_event({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 10, "total_tokens": 22}})
        if "断流" in prompt and self.attempts[prompt] == 1:
            return
        self.write_event({"choices": [{"delta": {"content": second}, "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def send_json(self, payload: dict[str, object]) -> None:
        """向浏览器测试的文件和视觉接口返回有长度边界的 JSON 响应。"""
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        """受控删除远端图片，避免测试文件进入真实服务。"""
        self.send_json({'deleted': True})

    def write_event(self, payload: dict[str, object]) -> None:
        """写入并刷新单个模型事件，让浏览器能在下一段之前观察界面。"""
        self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:
        """测试替身不打印模型请求或凭据。"""
        return


def main() -> None:
    """启动测试站点，退出时清理本进程创建的数据库与临时配置。"""
    admin = os.environ.get("TEST_DATABASE_URL", "postgresql://general_agent:local-development-only@127.0.0.1:15432/postgres")
    name = "general_agent_browser_" + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    model = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("TEST_MODEL_PORT", "18081"))), ControlledModel)
    Thread(target=model.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as directory:
            app = create_app(make_conninfo(admin, dbname=name), Path(directory))
            uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("TEST_APP_PORT", "18080")), log_level="warning")
    finally:
        model.shutdown()
        model.server_close()
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
