"""为候选卡片验收启动隔离真实 PG/Mem0 和受控外部模型服务。"""

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


class CandidateModel(ControlledModel):
    """按实际 HTTP 请求提供候选 JSON、嵌入及流式普通回答。"""

    def do_POST(self) -> None:
        """仅替换外部服务协议，候选确认、存储和预算仍运行真实产品模块。"""
        payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        if self.path.endswith('/embeddings'):
            result={'model':payload['model'],'data':[{'index':index,'embedding':[1.,0.,0.]} for index,_ in enumerate(payload['input'])],'usage':{'prompt_tokens':4,'total_tokens':4}}
        elif 'memory_candidate_discovery' in payload['messages'][0]['content']:
            context=json.loads(payload['messages'][1]['content'])
            quote=context['current_user_message']
            target=context['targets'][0] if quote=='最近在杭州' and context['targets'] else None
            item={'fact':'我住在杭州' if target else quote,'quote':quote,'subject':'user','target_id':target['id'] if target else None}
            result={'choices':[{'message':{'content':json.dumps({'candidates':[item]},ensure_ascii=False)}}],'usage':{'prompt_tokens':12,'completion_tokens':10,'total_tokens':22}}
        else:
            answer='普通测试回答。'
            for message in payload['messages']:
                if message['role']=='system' and '我喜欢简洁的中文回答' in message['content']:
                    answer='根据已批准记忆：我喜欢简洁的中文回答。'
            if payload.get('stream'):
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                self.write_event({'choices':[{'delta':{'content':answer},'finish_reason':'stop'}]})
                self.write_event({'choices':[],'usage':{'prompt_tokens':12,'completion_tokens':10,'total_tokens':22}})
                self.wfile.write(b'data: [DONE]\n\n')
                return
            result={'choices':[{'message':{'content':answer}}],'usage':{'prompt_tokens':12,'completion_tokens':10,'total_tokens':22}}
        body=json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    """只清理本进程创建的随机数据库与临时目录，端口由独立配置指定。"""
    admin=os.environ['TEST_DATABASE_URL']
    name='general_agent_candidates_browser_'+uuid4().hex
    with psycopg.connect(admin,autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    model=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('TEST_MODEL_PORT','18109'))),CandidateModel)
    Thread(target=model.serve_forever,daemon=True).start()
    try:
        with TemporaryDirectory() as directory:
            uvicorn.run(create_app(make_conninfo(admin,dbname=name),Path(directory)),host='127.0.0.1',port=int(os.environ.get('TEST_APP_PORT','18108')),log_level='warning')
    finally:
        model.shutdown()
        model.server_close()
        with psycopg.connect(admin,autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__=='__main__':
    main()
