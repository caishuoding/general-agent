"""通过受信任后端启动按 Agent Run 隔离的 Docker 执行环境。"""

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID


class DockerSandbox:
    """Run 只挂载自己的 workspace 和各已用 Skill 的模板副本。"""

    def __init__(self, directory: Path) -> None:
        """读取部署级固定镜像和宿主生成目录，不接受模型提供 Docker 参数。"""
        self.directory = directory.resolve()
        host_root = Path(os.environ.get('SKILL_HOST_DATA_DIR', str(self.directory.parent)))
        self.host_directory = host_root / 'generated'
        self.image = os.environ.get('SKILL_RUNTIME_IMAGE', 'general-agent-skills:5e8856be')
        self.network = os.environ.get('SKILL_RUNTIME_NETWORK', 'general-agent-skills')
        self.lock = threading.Lock()

    @staticmethod
    def name(run_id: UUID) -> str:
        """从不可伪造的 Agent Run UUID 构造固定容器名。"""
        return 'general-agent-skill-' + run_id.hex

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

    def host_path(self, path: Path) -> Path:
        """把应用容器内的生成路径映射到 Docker 宿主实际路径。"""
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.directory)
        except ValueError:
            raise ValueError('Skill 工作路径不属于生成目录。') from None
        return self.host_directory / relative

    def ensure(self, run_id: UUID, workspace: Path, libraries: dict[str, Path]) -> None:
        """幂等创建带限额容器；挂载集合变化时重建以支持后续新增 Skill。"""
        with self.lock:
            name = self.name(run_id)
            workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
            mount_args: list[str] = ['--mount', f'type=bind,src={self.host_path(workspace)},dst=/workspace']
            expected = {(str(self.host_path(workspace)), '/workspace')}
            for skill, library in sorted(libraries.items()):
                if library.is_dir() and not library.is_symlink():
                    source, destination = self.host_path(library), f'/opt/skills/{skill}/templates'
                    mount_args.extend(['--mount', f'type=bind,src={source},dst={destination}'])
                    expected.add((str(source), destination))
            try:
                state = self.control(['inspect', '--format', '{{.State.Running}}', name]).strip()
                raw = self.control(['inspect', '--format', '{{json .Mounts}}', name]).strip()
                actual = {(str(item.get('Source', '')), str(item.get('Destination', '')))
                          for item in json.loads(raw or b'[]')}
                if state == b'true' and actual == expected:
                    return
                self.control(['rm', '-f', name])
            except (ValueError, json.JSONDecodeError):
                try:
                    self.control(['rm', '-f', name])
                except ValueError:
                    pass
            proxy = 'http://skill-egress:3128'
            self.control(['run', '-d', '--name', name, '--label', 'general-agent.skill-run=true',
                          '--network', self.network, '--read-only', '--cap-drop', 'ALL',
                          '--security-opt', 'no-new-privileges', '--memory', '2g', '--cpus', '2',
                          '--pids-limit', '256', '--tmpfs', '/tmp:rw,nosuid,size=512m',
                          '--shm-size', '256m', *mount_args,
                          '-e', f'HTTP_PROXY={proxy}', '-e', f'HTTPS_PROXY={proxy}',
                          '-e', f'http_proxy={proxy}', '-e', f'https_proxy={proxy}',
                          '-e', 'NO_PROXY=localhost,127.0.0.1', self.image])

    def stop(self, run_id: UUID) -> None:
        """删除指定 Run 的容器，后台脚本和预览随之停止，workspace 仍保留。"""
        try:
            self.control(['rm', '-f', self.name(run_id)], timeout=10)
        except ValueError:
            pass

    def run(self, run_id: UUID, command: str, output: Path, guard: Callable[[], None],
            environment: dict[str, str], seconds: int, workspace: Path,
            libraries: dict[str, Path]) -> int:
        """执行一条 Run 命令并持续核验取消、时限和日志大小。"""
        guard()
        self.ensure(run_id, workspace, libraries)
        guard()
        args = ['docker', 'exec', '-i']
        for key, value in environment.items():
            args.extend(['-e', f'{key}={value}'])
        args.extend([self.name(run_id), '/bin/bash', '-lc', command])
        start = time.monotonic()
        with output.open('wb') as log:
            process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    guard()
                    if time.monotonic() - start > seconds or output.stat().st_size > 2_000_000:
                        raise ValueError('制作命令超时或输出超过限额。')
                    time.sleep(0.25)
                if output.stat().st_size > 2_000_000:
                    raise ValueError('制作命令输出超过限额。')
                guard()
                return process.returncode
            except BaseException:
                self.stop(run_id)
                process.kill()
                process.wait(timeout=10)
                raise

    def proxy(self, run_id: UUID, port: int, method: str, path: str, body: bytes,
              content_type: str) -> dict[str, Any]:
        """只代理 Run 容器内的回环预览端口，响应通过 JSON/base64 安全传输。"""
        import base64
        payload = json.dumps({'port': port, 'method': method, 'path': path,
                              'body': base64.b64encode(body).decode(),
                              'content_type': content_type}).encode()
        script = (
            'import urllib.request,urllib.error,urllib.parse,json,base64,sys; p=json.load(sys.stdin); '
            'r=urllib.request.Request("http://127.0.0.1:"+str(p["port"])+urllib.parse.quote(p["path"],safe="/%?=&:+,;@"), '
            'data=base64.b64decode(p["body"]) if p["method"]!="GET" else None, '
            'method=p["method"],headers={"Content-Type":p["content_type"]}); '
            'o=urllib.request.build_opener(urllib.request.ProxyHandler({})); '
            '\ntry: s=o.open(r,timeout=8)\nexcept urllib.error.HTTPError as e: s=e\n'
            'b=s.read(10000001); assert len(b)<=10000000; '
            'print(json.dumps({"status":s.status,"content_type":s.headers.get("Content-Type","application/octet-stream"),'
            '"body":base64.b64encode(b).decode()}))'
        )
        response = self.control(['exec', '-i', self.name(run_id), 'python', '-c', script], payload)
        return dict(json.loads(response))
