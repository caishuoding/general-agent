"""通过受信任后端启动 Docker 隔离任务，模型命令永远不在应用进程执行。"""

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID


class DockerSandbox:
    """任务只挂载自己的工作文件及模板库，使用内部网络和公网代理。"""

    def __init__(self, data_directory: Path) -> None:
        """读取部署级固定镜像和宿主数据路径，不接受模型提供 Docker 参数。"""
        self.directory = data_directory.resolve()
        self.host_directory = Path(os.environ.get('SKILL_HOST_DATA_DIR', str(self.directory)))
        self.image = os.environ.get('SKILL_RUNTIME_IMAGE', 'general-agent-skills:5e8856be')
        self.network = os.environ.get('SKILL_RUNTIME_NETWORK', 'general-agent-skills')
        self.lock = threading.Lock()

    @staticmethod
    def name(task: UUID) -> str:
        """从不可伪造任务 UUID 构造固定容器名。"""
        return 'general-agent-skill-' + task.hex

    def control(self, arguments: list[str], data: bytes | None = None, timeout: int = 20) -> bytes:
        """执行固定 Docker 管理参数，命令错误不回传可能包含配置的原始 stderr。"""
        try:
            result = subprocess.run(['docker', *arguments], input=data, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Skill 隔离环境不可用，请检查 Docker、运行镜像和内部网络配置。') from None
        if result.returncode:
            raise ValueError('Skill 容器操作失败，请检查运行镜像、宿主数据路径及 Docker 权限。')
        return result.stdout

    def ensure(self, task: UUID, skill: str | None = None) -> None:
        """幂等创建带限额的任务容器，不挂载其他任务、应用目录或 Docker socket。"""
        with self.lock:
            name = self.name(task)
            try:
                state = self.control(['inspect', '--format', '{{.State.Running}}', name]).strip()
                if state == b'true':
                    return
                self.control(['rm', '-f', name])
            except ValueError:
                pass
            workspace = self.directory / 'skill-tasks' / str(task) / 'workspace'
            workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
            host = self.host_directory / 'skill-tasks' / str(task) / 'workspace'
            library = self.directory / 'skill-tasks' / str(task) / 'library'
            host_library = self.host_directory / 'skill-tasks' / str(task) / 'library'
            mounts = (['--mount', f'type=bind,src={host_library},dst=/opt/skills/{skill}/templates']
                if skill is not None and library.is_dir() and not library.is_symlink() else [])
            proxy = 'http://skill-egress:3128'
            self.control(['run', '-d', '--name', name, '--label', 'general-agent.skill-task=true',
                '--network', self.network, '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--memory', '2g', '--cpus', '2',
                '--pids-limit', '256', '--tmpfs', '/tmp:rw,nosuid,size=512m',
                '--shm-size', '256m', '--mount', f'type=bind,src={host},dst=/workspace',
                *mounts,
                '-e', f'HTTP_PROXY={proxy}', '-e', f'HTTPS_PROXY={proxy}',
                '-e', f'http_proxy={proxy}', '-e', f'https_proxy={proxy}',
                '-e', 'NO_PROXY=localhost,127.0.0.1', self.image])

    def stop(self, task: UUID) -> None:
        """删除指定任务的运行容器，后台脚本和预览随之停止，工作文件仍保留。"""
        try:
            self.control(['rm', '-f', self.name(task)], timeout=10)
        except ValueError:
            pass

    def run(self, task: UUID, command: str, output: Path, guard: Callable[[], None],
            environment: dict[str, str], seconds: int = 300, skill: str | None = None) -> int:
        """执行单条任务命令并持续核验取消、时限和日志大小，失败时终止整个任务容器。"""
        guard()
        self.ensure(task, skill)
        guard()
        args = ['docker', 'exec', '-i']
        for key, value in environment.items():
            args.extend(['-e', f'{key}={value}'])
        args.extend([self.name(task), '/bin/bash', '-lc', command])
        start = time.monotonic()
        with output.open('wb') as log:
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    guard()
                    if time.monotonic() - start > seconds or output.stat().st_size > 2_000_000:
                        raise ValueError('制作命令超时或输出超过限额。')
                    time.sleep(0.25)
                guard()
                return process.returncode
            except BaseException:
                self.stop(task)
                process.kill()
                process.wait(timeout=10)
                raise

    def proxy(self, task: UUID, port: int, method: str, path: str, body: bytes, content_type: str) -> dict[str, Any]:
        """只代理任务容器内的回环预览端口，响应通过 JSON/base64 安全传输。"""
        import base64
        import json
        payload = json.dumps({'port': port, 'method': method, 'path': path,
            'body': base64.b64encode(body).decode(), 'content_type': content_type}).encode()
        script = (
            'import urllib.request,urllib.error,urllib.parse,json,base64,sys; p=json.load(sys.stdin); '
            'r=urllib.request.Request("http://127.0.0.1:"+str(p["port"])+urllib.parse.quote(p["path"],safe="/%?=&:+,;@"), '
            'data=base64.b64decode(p["body"]) if p["method"]!="GET" else None, '
            'method=p["method"],headers={"Content-Type":p["content_type"]}); '
            'o=urllib.request.build_opener(urllib.request.ProxyHandler({})); '
            '\ntry: s=o.open(r,timeout=8)\nexcept urllib.error.HTTPError as e: s=e\n'
            'b=s.read(10000001); '
            'assert len(b)<=10000000; '
            'print(json.dumps({"status":s.status,"content_type":s.headers.get("Content-Type","application/octet-stream"),"body":base64.b64encode(b).decode()}))'
        )
        response = self.control(['exec', '-i', self.name(task), 'python', '-c', script], payload)
        return dict(json.loads(response))
