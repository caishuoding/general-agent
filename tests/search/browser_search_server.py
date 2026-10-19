"""为模型自主联网浏览器流程启动隔离模型、搜索替身与真实 PostgreSQL。"""

import json
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tests.conversation.browser_server import ControlledModel
from general_agent.app import create_app


class SearchModel(ControlledModel):
    """提供 search_web 工具调用和带实际网页出处的回答，不接真实网络搜索。"""

    def do_POST(self) -> None:
        """按工具协议返回查询、智谱结果或最终回答，仅供受控浏览器验证。"""
        payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        body: dict[str, Any]
        if self.path=='/web_search':
            assert payload['search_query']=='上海市青松路 88 号附近咖啡店'
            assert payload['search_engine']=='search_std'
            body={'search_result':[{'link':'https://cafe.example/nearby','title':'咖啡店信息','content':'营业时间 9–18。'}]}
        elif any(message.get('role')=='tool' for message in payload['messages']):
            source=json.loads(payload['messages'][-1]['content'])['results'][0]
            answer=f'咖啡店营业时间 9–18。[{source["key"]}]'
            if payload.get('stream'):
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                self.write_event({'choices':[{'delta':{'content':'咖啡店营业时间'}}]})
                sleep(0.4)
                self.write_event({'choices':[{'delta':{'content':f' 9–18。[{source["key"]}]'},'finish_reason':'stop'}]})
                self.write_event({'choices':[],'usage':{'prompt_tokens':30,'completion_tokens':10,'total_tokens':40}})
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
                return
            body={'choices':[{'message':{'role':'assistant','content':answer}}],'usage':{'prompt_tokens':30,'completion_tokens':10,'total_tokens':40}}
        else:
            assert '上海市青松路 88 号' in str(payload['messages'])
            if payload.get('stream'):
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                self.write_event({'choices':[{'delta':{'tool_calls':[{'index':0,'id':'call-search','type':'function','function':{'name':'search_web','arguments':''}}]}}]})
                self.write_event({'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':json.dumps({'query':'上海市青松路 88 号附近咖啡店'},ensure_ascii=False)}}]}}]})
                self.write_event({'choices':[{'delta':{},'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}})
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
                return
            body={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'call-search','type':'function','function':{'name':'search_web','arguments':json.dumps({'query':'上海市青松路 88 号附近咖啡店'},ensure_ascii=False)}}]}}],'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}}
        encoded=json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    """管理本次随机数据库和临时配置，退出后只清理自己创建的资源。"""
    admin=os.environ.get('TEST_DATABASE_URL','postgresql://general_agent:local-development-only@127.0.0.1:15432/postgres')
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
