"""通过隔离执行公开接口验证真实 Docker 命令和取消行为。"""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from general_agent.skills.sandbox import DockerSandbox

pytestmark = pytest.mark.skipif(os.environ.get('TEST_SKILL_DOCKER') != '1', reason='需先运行 setup-skills.sh 并设置 TEST_SKILL_DOCKER=1')


def test_command_runs_with_only_its_workspace_and_stops_on_guard(tmp_path: Path) -> None:
    """真实任务容器可写工作文件，但看不到宿主应用和 Docker socket，并能被取消。"""
    sandbox = DockerSandbox(tmp_path)
    identifier = uuid4()

    def allowed() -> None:
        """允许首个命令完成，由测试后续显式撤销执行许可。"""
        return

    try:
        code = sandbox.run(identifier,
            'test ! -e /app/.local/model.json && test ! -e /var/run/docker.sock && test -z "$DATABASE_URL" && printf safe > /workspace/result.txt',
            tmp_path / 'command.log', allowed, {}, 15)
        assert code == 0
        assert (tmp_path / 'skill-tasks' / str(identifier) / 'workspace' / 'result.txt').read_text() == 'safe'

        def cancelled() -> None:
            """模拟用户停止，隔离执行必须终止而非等待脚本自然完成。"""
            raise ValueError('用户已停止')

        with pytest.raises(ValueError, match='用户已停止'):
            sandbox.run(identifier, 'sleep 60', tmp_path / 'cancel.log', cancelled, {}, 90)
    finally:
        sandbox.stop(identifier)
