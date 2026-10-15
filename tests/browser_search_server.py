"""为搜索确认浏览器流程启动隔离模型、搜索替身与真实 PostgreSQL。"""

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


class SearchModel(ControlledModel):
    """提供查询规划和带实际网页出处的回答，不接真实网络搜索。"""

    def do_POST(self) -> None:
        """按真实协议返回查询、Tavily结果或最终回答，仅供受控浏览器验证。"""
        payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        if self.path=='/search':
            assert payload['query']=='上海市青松路 88 号附近咖啡店'
            body={'results':[{'url':'https://cafe.example/nearby','title':'咖啡店信息','content':'营业时间 9–18。'}],'usage':{'credits':1}}
        elif payload['messages'][0]['content'].startswith('为明确联网请求生成查询'):
            assert '上海市青松路 88 号' in str(payload['messages'])
            body={'choices':[{'message':{'content':json.dumps({'query':'上海市青松路 88 号附近咖啡店'},ensure_ascii=False)}}],'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}}
        else:
            sources=json.loads(payload['messages'][0]['content'].split('\n',1)[1])
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.end_headers()
            self.write_event({'choices':[{'delta':{'content':f'咖啡店营业时间 9–18。[{sources[0]["key"]}]'},'finish_reason':'stop'}],'usage':{'prompt_tokens':30,'completion_tokens':10,'total_tokens':40}})
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            return
        encoded=json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    """管理本次随机数据库和临时配置，退出后只清理自己创建的资源。"""
    admin=os.environ.get('TEST_DATABASE_URL','postgresql://general_agent:local-development-only@127.0.0.1:15433/postgres')
    name='general_agent_search_browser_'+uuid4().hex
    with psycopg.connect(admin,autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    servers=[ThreadingHTTPServer(('127.0.0.1',int(os.environ.get(variable,default))),SearchModel) for variable,default in [('TEST_MODEL_PORT','18103'),('TEST_SEARCH_PORT','18104')]]
    for server in servers:
        Thread(target=server.serve_forever,daemon=True).start()
    try:
        with TemporaryDirectory() as directory:
            uvicorn.run(create_app(make_conninfo(admin,dbname=name),Path(directory)),host='127.0.0.1',port=int(os.environ.get('TEST_APP_PORT','18102')),log_level='warning')
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        with psycopg.connect(admin,autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__=='__main__':
    main()
