"""为浏览器测试启动隔离数据库、临时配置和受控模型 HTTP 服务。"""

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from general_agent.app import create_app


class ControlledModel(BaseHTTPRequestHandler):
    """只在测试进程中运行的模型替身，不用于产品演示或真实验收。"""

    def do_POST(self) -> None:
        """消费模型请求并返回固定的文字响应。"""
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.dumps({"choices": [{"message": {"content": "这是受控模型的测试回答。"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 10, "total_tokens": 22}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """测试替身不打印模型请求或凭据。"""
        return


def main() -> None:
    """启动测试站点，退出时清理本进程创建的数据库与临时配置。"""
    admin = os.environ.get("TEST_DATABASE_URL", "postgresql://general_agent:local-development-only@127.0.0.1:15432/postgres")
    name = "general_agent_browser_" + uuid4().hex
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    model = ThreadingHTTPServer(("127.0.0.1", 18081), ControlledModel)
    Thread(target=model.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as directory:
            app = create_app(make_conninfo(admin, dbname=name), Path(directory))
            uvicorn.run(app, host="127.0.0.1", port=18080, log_level="warning")
    finally:
        model.shutdown()
        model.server_close()
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()
