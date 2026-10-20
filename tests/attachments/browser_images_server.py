"""运行图片浏览器验收需要的独立真实 PostgreSQL 与受控视觉 HTTP 服务。"""

import json
import os
import sys
import time
from email.parser import BytesParser
from email.policy import default
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tests.conversation.browser_server import ControlledModel
from general_agent.app import create_app


class ImageModel(ControlledModel):
    """提供受控 Files API 和 file_id 视觉回答，不调用真实 DeepSeek。"""

    def do_POST(self) -> None:
        """验证 multipart 的 30 天 TTL，上传后只接受 file_id 图片输入。"""
        raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if self.path == '/files':
            form = BytesParser(policy=default).parsebytes(
                b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + raw,
            )
            fields = {part.get_param('name', header='content-disposition'): part for part in form.iter_parts()}
            assert fields['purpose'].get_payload(decode=True) == b'user_data'
            assert fields['expires_after[anchor]'].get_payload(decode=True) == b'created_at'
            assert fields['expires_after[seconds]'].get_payload(decode=True) == b'2592000'
            image = fields['file'].get_payload(decode=True)
            assert isinstance(image, bytes) and image.startswith(b'\x89PNG')
            body = json.dumps({'id': 'file-api-browser-image', 'object': 'file', 'created_at': int(time.time()),
                               'expires_at': int(time.time()) + 2592000}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        payload = json.loads(raw)
        parts = [part for message in payload['messages'] if isinstance(message['content'], list) for part in message['content']]
        assert {'type': 'file', 'file_id': 'file-api-browser-image'} in parts
        assert not any(part['type'] == 'image_url' for part in parts)
        if not payload.get('stream'):
            body = json.dumps({'choices': [{'message': {'content': 'red'}}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 1, 'total_tokens': 21}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        answer = '受控视觉服务返回红色。'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.write_event({'choices': [{'delta': {'content': answer}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 40, 'completion_tokens': 10, 'total_tokens': 50}})
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


def main() -> None:
    """仅启动并清理本进程随机数据库和配置，不读取用户设置或修改共享库。"""
    admin = os.environ.get('TEST_DATABASE_URL', 'postgresql://general_agent:local-development-only@127.0.0.1:15432/postgres')
    name = 'general_agent_images_browser_' + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    model = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('TEST_MODEL_PORT', '18097'))), ImageModel)
    Thread(target=model.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as directory:
            app = create_app(make_conninfo(admin, dbname=name), Path(directory))
            uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('TEST_APP_PORT', '18096')), log_level='warning')
    finally:
        model.shutdown()
        model.server_close()
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__ == '__main__':
    main()
