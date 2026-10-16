"""为原生自动记忆浏览器验收提供真实数据库、Mem0 和受控模型 HTTP 服务。"""

import os
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

import httpx
import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

from browser_server import ControlledModel
from general_agent.app import create_app
from test_native_memory import native_provider, json_reply


class NativeModel(ControlledModel):
    """只替换云端 HTTP，实际产品负责原生记忆、工具调用和管理操作。"""

    def do_POST(self) -> None:
        """将浏览器应用的真实模型请求交给统一受控协议，透传正文及流式内容类型。"""
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        payload = json.loads(body)
        response = json_reply('invalid JSON') if payload.get('model') == 'bad-json' and payload.get('response_format') else native_provider(httpx.Request('POST', 'https://model.example'+self.path, content=body))
        self.send_response(response.status_code)
        self.send_header('Content-Type', response.headers.get('content-type', 'application/json'))
        self.send_header('Content-Length', str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)


def main() -> None:
    """只创建和清理当前进程的随机测试库、配置目录与模型监听端口。"""
    admin = os.environ['TEST_DATABASE_URL']
    name = 'general_agent_native_browser_'+uuid4().hex
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    model = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('TEST_MODEL_PORT', '18109'))), NativeModel)
    Thread(target=model.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as directory:
            uvicorn.run(create_app(make_conninfo(admin, dbname=name), Path(directory)), host='127.0.0.1', port=int(os.environ.get('TEST_APP_PORT', '18108')), log_level='warning')
    finally:
        model.shutdown()
        model.server_close()
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__ == '__main__':
    main()
