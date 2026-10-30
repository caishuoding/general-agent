"""通过聊天和用户文件接口验证上游原生导出与预览标注完整闭环。"""

import json
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from general_agent.skills.sandbox import DockerSandbox
from tests.agent.test_multi_agent import response
from tests.auth.support import authenticated_client, set_global_settings

pytestmark = pytest.mark.skipif(os.environ.get('TEST_SKILL_DOCKER') != '1', reason='需要完整 Skill Docker 运行环境')

SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720" lang="en-US" data-pptx-page-role="content">
<rect id="background" x="0" y="0" width="1280" height="720" fill="#FFFFFF" data-pptx-role="background"/>
<g id="headline" data-pptx-bounds="80 80 1100 180"><text id="title" x="80" y="160" font-family="Liberation Serif" font-size="48" fill="#17365D">Editable presentation</text></g>
<g id="diagram" data-pptx-bounds="80 300 400 180"><rect x="80" y="300" width="400" height="180" rx="16" fill="#2863A4"/></g></svg>'''


def verify_browser_preview(client: TestClient, url: str, screenshot: Path) -> None:
    """通过本机 HTTP 转发让真实浏览器验证不透明源预览页面的脚本、样式与取图。"""
    if os.environ.get('TEST_SKILL_BROWSER') != '1':
        return
    node = shutil.which('node')
    assert node is not None, '浏览器验收需要 Node.js'

    class Bridge(BaseHTTPRequestHandler):
        """仅在测试内把浏览器请求转给已启动的真实应用接口。"""

        def log_message(self, format: str, *args: object) -> None:
            """测试不记录包含临时预览令牌的地址。"""
            return

        def do_GET(self) -> None:
            """转发页面、样式、脚本和实际 SVG 内容。"""
            response = client.get(self.path, headers={'origin': self.headers.get('origin', '')})
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in {'content-length', 'transfer-encoding'}:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Bridge)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = '''const {chromium}=require('@playwright/test');
// 在真实浏览器中确认预览脚本能加载中文页面并渲染原生 SVG。
async function verify(){
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();
  await page.goto(process.argv[1]);
  await page.locator('#svg-content svg').waitFor({timeout:15000});
  await page.locator('#svg-content #title').waitFor();
  await page.screenshot({path:process.argv[2]});
 } finally {await browser.close();}
}
// 将验收失败转换为进程退出码并保留错误。
verify().catch(function report(error){console.error(error);process.exit(1);});'''
    try:
        result = subprocess.run([node, '-e', script, f'http://127.0.0.1:{server.server_port}' + url, str(screenshot)],
            cwd=Path('frontend'), capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_agent_exports_native_pptx_and_user_can_annotate_preview(database_url: str, tmp_path: Path) -> None:
    """受控模型驱动真实上游脚本，用户下载原生 PPT 并通过代理保存预览标注。"""
    task_id, command_id, download_url, preview_url, confirmation_url = '', '', '', '', ''
    phase = 0
    script = (
        'from pathlib import Path\nimport subprocess,json\n'
        'project=Path("/workspace/deck")\n(project/"svg_output").mkdir(parents=True,exist_ok=True)\n'
        f'(project/"svg_output/01_封面.svg").write_text({SVG!r})\n'
        'scripts=Path("/opt/skills/ppt-master/scripts")\n'
        'subprocess.run(["python",str(scripts/"attribution_guard.py")],check=True)\n'
        'subprocess.run(["python",str(scripts/"svg_quality_checker.py"),str(project),"--quick-generate","--stage","final","--json"],check=True)\n'
        'subprocess.run(["python",str(scripts/"finalize_svg.py"),str(project)],check=True)\n'
        'subprocess.run(["python",str(scripts/"svg_to_pptx.py"),str(project),"--quick-generate","--no-notes","-o",str(project/"exports/report.pptx")],check=True)\n'
        'subprocess.run(["python",str(scripts/"svg_editor/server.py"),str(project),"--daemon","--no-browser"],check=True)\n'
        '(project/"confirm_ui").mkdir(exist_ok=True)\n'
        '(project/"confirm_ui/recommendations.stage1.json").write_text(json.dumps({"stage":"stage1","lang":"zh","primary_language":"zh-CN","recommend":{"canvas":"ppt169"},"audience":{"value":"管理层"},"communication_intent":{"value":"汇报"},"audience_outcome":{"value":"了解进展"},"core_message":{"value":"进展正常"},"delivery_context":{"value":"现场汇报"},"artifact_afterlife":{"value":"存档"},"content_divergence":{"value":""},"template_options":{"schema_version":1,"phase":"template","lang":"zh","default_mode":"free_design","explicit_workspace_roots":[]}}))\n'
        'subprocess.run(["python",str(scripts/"confirm_ui/server.py"),str(project),"--daemon","--no-browser"],check=True)\n'
    )

    def provider(request: httpx.Request) -> httpx.Response:
        """按工具真实结果推进制作，不把启动命令视为已经导出成功。"""
        nonlocal phase, task_id, command_id, download_url, preview_url, confirmation_url
        payload: dict[str, Any] = json.loads(request.content)
        messages = [json.loads(item['content']) for item in payload['messages'] if item['role'] == 'tool']
        if messages:
            assert not messages[-1].get('error'), messages[-1]
        if phase == 0:
            phase = 1
            return response(name='load_skill', arguments={'name': 'ppt-master'})
        if phase == 1:
            task_id = messages[-1]['task_id']
            phase = 2
            return response(name='write_work_file', arguments={'task_id': task_id, 'path': 'render.py', 'content': script})
        if phase == 2:
            phase = 3
            return response(name='run_skill_command', arguments={'task_id': task_id, 'command': 'python /workspace/render.py', 'seconds': 120})
        if phase == 3:
            command_id = messages[-1]['command_id']
            phase = 4
        if phase == 4:
            if messages[-1].get('status') != 'completed':
                assert messages[-1].get('status') not in {'interrupted', 'failed'}, messages[-1]
                return response(name='poll_skill_command', arguments={'task_id': task_id, 'command_id': command_id, 'wait_seconds': 10})
            assert messages[-1]['exit_code'] == 0, messages[-1]
            phase = 5
            return response(name='publish_skill_file', arguments={'task_id': task_id, 'path': 'deck/exports/report.pptx'})
        if phase == 5:
            download_url = messages[-1]['url']
            phase = 6
            return response(name='open_skill_preview', arguments={'task_id': task_id, 'project_path': 'deck'})
        if phase == 6:
            preview_url = messages[-1]['url']
            phase = 7
            return response(name='open_skill_preview', arguments={'task_id': task_id, 'project_path': 'deck', 'surface': 'confirmation'})
        if phase == 7:
            confirmation_url = messages[-1]['url']
            phase = 8
            return response(name='save_skill_task', arguments={'task_id': task_id, 'status': 'completed', 'stage': '已通过上游检查并导出', 'route': 'generate'})
        return response(f'制作完成。[下载 PPT]({download_url}) [预览]({preview_url})')

    set_global_settings(database_url, tmp_path, {'base_url': 'https://model.example/v1', 'model': 'controlled',
        'api_key': 'test-key', 'context_window_tokens': 262144})
    try:
        with authenticated_client(database_url, tmp_path, httpx.MockTransport(provider)) as client:
            conversation = client.post('/api/conversations', json={}).json()['id']
            result = client.post(f'/api/conversations/{conversation}/messages', json={'content': '直接制作一页验证 PPT'})
            assert result.status_code == 200, result.text
            assert result.json()['messages'][-1]['status'] == 'complete', result.text
            with ZipFile(BytesIO(client.get(download_url).content)) as package:
                slide = package.read('ppt/slides/slide1.xml').decode()
                assert 'Editable presentation' in slide
                assert '<p:sp>' in slide
                assert '<p:pic>' not in slide
            redirected = client.get(preview_url, follow_redirects=False)
            assert redirected.status_code == 303, redirected.text
            url = redirected.headers['location']
            page = client.get(url)
            assert page.status_code == 200
            assert url + 'static/app.js' in page.text
            assert 'allow-same-origin' not in page.headers['content-security-policy']
            verify_browser_preview(client, url, tmp_path / 'preview.png')
            slides = client.get(url + 'api/slides')
            assert slides.status_code == 200, slides.text
            changed = client.post(url + 'api/slide/01_封面.svg/annotate', json={'element_id': 'title', 'annotation': '标题改成蓝色'})
            assert changed.status_code == 200, changed.text
            assert changed.json()['draft_saved'] is True
            confirmation = client.get(confirmation_url, follow_redirects=False)
            assert confirmation.status_code == 303, confirmation.text
            confirm_root = confirmation.headers['location']
            assert client.get(confirm_root).status_code == 200
            recommendations = client.get(confirm_root + 'api/recommendations')
            assert recommendations.status_code == 200, recommendations.text
            assert '管理层' in recommendations.text
    finally:
        if task_id:
            DockerSandbox(tmp_path).stop(UUID(task_id))
