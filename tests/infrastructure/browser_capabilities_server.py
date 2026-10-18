"""为能力设置浏览器流程提供独立端口、真实数据库与受控模型服务。"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from general_agent.app import create_app


class ControlledCapabilities(BaseHTTPRequestHandler):
    """返回可复现的分项能力结果，不连接任何真实云端服务。"""

    def do_POST(self) -> None:
        """按实际协议返回文字、图片、嵌入成功与工具缺失结果。"""
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path == "/v1/embeddings":
            payload = {"model": request["model"], "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}], "usage": {"prompt_tokens": 4, "total_tokens": 4}}
        else:
            content = "RED" if isinstance(request["messages"][0]["content"], list) else "OK"
            payload = {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """关闭替身访问日志，避免打印请求和凭据。"""
        return


def main() -> None:
    """按测试端口启动临时能力站点，退出时只删除本进程创建的数据库。"""
    admin = os.environ.get("TEST_DATABASE_URL", "postgresql://general_agent:local-development-only@127.0.0.1:15432/postgres")
    name = "general_agent_capabilities_browser_" + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    model = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("TEST_MODEL_PORT", "18085"))), ControlledCapabilities)
    Thread(target=model.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as directory:
            app = create_app(make_conninfo(admin, dbname=name), Path(directory))
            uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("TEST_APP_PORT", "18084")), log_level="warning")
    finally:
        model.shutdown()
        model.server_close()
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
