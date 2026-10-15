"""运行图片浏览器验收需要的独立真实 PostgreSQL 与受控视觉 HTTP 服务。"""

import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

from browser_server import ControlledModel
from general_agent.app import create_app


class ImageModel(ControlledModel):
    """仅对真实多模态载荷返回图片依据，并支持基础红色能力检查。"""

    def do_POST(self) -> None:
        """识别测试图片请求形状并返回带真实来源标识的可计量流式回答。"""
        payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        parts = [part for message in payload['messages'] if isinstance(message['content'], list) for part in message['content']]
        assert any(part['type'] == 'image_url' and part['image_url']['url'].startswith('data:image/png;base64,') for part in parts)
        if not payload.get('stream'):
            body = json.dumps({'choices': [{'message': {'content': 'red'}}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 1, 'total_tokens': 21}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        sources = json.loads(payload['messages'][0]['content'].split('\n', 1)[1])
        answer = '受控视觉服务返回红色。' + ''.join(f'[{source["key"]}]' for source in sources)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.write_event({'choices': [{'delta': {'content': answer}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 40, 'completion_tokens': 10, 'total_tokens': 50}})
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


def main() -> None:
    """仅启动并清理本进程随机数据库和配置，不读取用户设置或修改共享库。"""
    admin = os.environ.get('TEST_DATABASE_URL', 'postgresql://general_agent:local-development-only@127.0.0.1:15433/postgres')
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
