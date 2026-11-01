"""通过隔离执行公开接口验证会话 workspace、模板副本和取消行为。"""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from general_agent.skills.sandbox import DockerSandbox

pytestmark = pytest.mark.skipif(os.environ.get('TEST_SKILL_DOCKER') != '1', reason='需先运行 setup-skills.sh 并设置 TEST_SKILL_DOCKER=1')


def test_command_runs_with_only_its_run_workspace_and_stops_on_guard(tmp_path: Path) -> None:
    """真实 Run 容器可写自己的 workspace，看不到宿主应用和 Docker socket，并能取消。"""
    generated = tmp_path / 'generated'
    sandbox = DockerSandbox(generated)
    run_id = uuid4()
    workspace = generated / 'account' / 'conversation' / str(run_id) / 'workspace'

    def allowed() -> None:
        """允许首个命令完成，由测试后续模拟取消。"""
        return

    try:
        code = sandbox.run(run_id,
            'test ! -e /app/.local/model.json && test ! -e /var/run/docker.sock && test -z "$DATABASE_URL" && printf safe > /workspace/result.txt',
            tmp_path / 'command.log', allowed, {}, 15, workspace, {})
        assert code == 0
        assert (workspace / 'result.txt').read_text() == 'safe'

        def cancelled() -> None:
            """模拟用户停止，隔离执行必须终止而不是等待脚本自然结束。"""
            raise ValueError('用户已停止')

        with pytest.raises(ValueError, match='用户已停止'):
            sandbox.run(run_id, 'sleep 60', tmp_path / 'cancel.log', cancelled, {}, 90, workspace, {})
    finally:
        sandbox.stop(run_id)
