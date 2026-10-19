"""为记忆浏览器测试启动真实 PG 与具备嵌入、流式响应的受控服务。"""

import json
import os
import sys
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


class MemoryModel(ControlledModel):
    """沿用分段回答协议并增加确定的嵌入服务。"""

    def do_POST(self) -> None:
        """为嵌入返回真实形状和用量，其余请求复用已有受控聊天。"""
        if self.path != '/v1/embeddings':
            super().do_POST()
            return
        request = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        body = json.dumps({'model':request['model'],'data':[{'index':i,'embedding':[1.,0.,0.]} for i,_ in enumerate(request['input'])],'usage':{'prompt_tokens':4,'total_tokens':4}}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    """只管理当前进程随机测试库与临时目录，禁止重启共享数据库。"""
    admin = os.environ.get('TEST_DATABASE_URL','postgresql://general_agent:local-development-only@127.0.0.1:15432/postgres')
    name = 'general_agent_memory_browser_'+uuid4().hex
    with psycopg.connect(admin,autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    model = ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('TEST_MODEL_PORT','18089'))),MemoryModel)
    Thread(target=model.serve_forever,daemon=True).start()
    try:
        with TemporaryDirectory() as directory:
            app = create_app(make_conninfo(admin,dbname=name),Path(directory))
            uvicorn.run(app,host='127.0.0.1',port=int(os.environ.get('TEST_APP_PORT','18088')),log_level='warning')
    finally:
        model.shutdown()
        model.server_close()
        with psycopg.connect(admin,autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__ == '__main__':
    main()
