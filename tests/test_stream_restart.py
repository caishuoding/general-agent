"""以真实进程重启验证中断记录，不直接操作数据库业务表。"""

import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import httpx


def test_killed_process_recovers_partial_answer_as_failed(database_url: str, tmp_path: Path) -> None:
    """强制终止生成中的后端并重启后，部分内容仍在且不会遗留虚假的生成中状态。"""
    release = Event()

    class Provider(BaseHTTPRequestHandler):
        """只响应本测试的模型请求，并将外部流停在第一段。"""

        def do_POST(self) -> None:
            """发送部分回答后等待测试清理，模拟尚未完成的真实网络连接。"""
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"persisted partial"}}]}\n\n')
            self.wfile.flush()
            release.wait(15)

        def log_message(self, format: str, *args: object) -> None:
            """测试服务不打印模型请求与认证信息。"""
            return

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    Thread(target=provider.serve_forever, daemon=True).start()
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        app_port = reserved.getsockname()[1]
    environment = {**os.environ, "DATABASE_URL": database_url, "APP_DATA_DIR": str(tmp_path), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "backend")}

    def launch() -> subprocess.Popen[bytes]:
        """在临时配置和测试数据库上启动独立后端进程。"""
        return subprocess.Popen([sys.executable, "-m", "uvicorn", "general_agent.app:create_app", "--factory", "--host", "127.0.0.1", "--port", str(app_port), "--log-level", "error"], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def ready(client: httpx.Client) -> None:
        """等待公开健康接口成功，超时明确终止测试。"""
        deadline = monotonic() + 10
        while monotonic() < deadline:
            try:
                if client.get("/api/health").status_code == 200:
                    return
            except httpx.RequestError:
                pass
            sleep(0.05)
        raise AssertionError("隔离后端未就绪")

    process = launch()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{app_port}", trust_env=False, timeout=2) as client:
            ready(client)
            client.put("/api/settings", json={"base_url": f"http://127.0.0.1:{provider.server_port}/v1", "model": "controlled", "api_key": "test-key"})
            conversation_id = client.post("/api/conversations", json={}).json()["id"]
            client.post(f"/api/conversations/{conversation_id}/runs", json={"content": "hello"})
            deadline = monotonic() + 5
            while monotonic() < deadline:
                message = client.get(f"/api/conversations/{conversation_id}").json()["messages"][-1]
                if message["content"] == "persisted partial":
                    break
                sleep(0.02)
            assert (message["status"], message["content"]) == ("generating", "persisted partial")
            process.kill()
            process.wait(timeout=5)
            process = launch()
            ready(client)
            restored = client.get(f"/api/conversations/{conversation_id}").json()["messages"][-1]
            assert (restored["status"], restored["content"]) == ("failed", "persisted partial")
            assert "重启" in restored["error"]
            usage = client.get("/api/usage").json()[0]
            assert usage["status"] == "failed"
            assert usage["total_tokens"] is None
    finally:
        process.terminate()
        process.wait(timeout=5)
        release.set()
        provider.shutdown()
        provider.server_close()
