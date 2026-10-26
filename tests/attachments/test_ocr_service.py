"""验证独立 OCR HTTP 边界把固定版本 Paddle 结果转换为业务应用可读取的协议。"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from typing import Any
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from ocr.server import create_app


class NativeResult:
    """模拟第三方模型公开的 json 属性，不替换业务解析或切片算法。"""

    json = {'res': {'parsing_res_list': [
        {'block_label': 'text', 'block_bbox': [10, 20, 200, 60], 'block_content': '原文 R-2048', 'block_order': 1},
    ], 'overall_ocr_res': {'rec_scores': [.97]}}}


class ExternalPipeline:
    """作为 Paddle 模型 SDK 的外部替身返回固定原生结构。"""

    def predict(self, path: str) -> Iterator[NativeResult]:
        """读取服务实际传入的图片，确保业务端尺寸与裁剪画布一致。"""
        with Image.open(path) as image:
            assert image.size == (300, 400)
        yield NativeResult()


class BlockingPipeline(ExternalPipeline):
    """第一次推理阻塞，验证并发请求会排队而不是立即返回 503。"""

    def __init__(self, entered: Event, release: Event) -> None:
        """保存测试同步事件和只允许首次调用阻塞的计数器。"""
        self.entered = entered
        self.release = release
        self.guard = Lock()
        self.calls = 0

    def predict(self, path: str) -> Iterator[NativeResult]:
        """首个请求等待释放，后续请求直接完成，证明锁采用有界等待。"""
        with self.guard:
            self.calls += 1
            first = self.calls == 1
        if first:
            self.entered.set()
            assert self.release.wait(10)
        yield NativeResult()


def test_ocr_service_exports_ordered_blocks_and_frame() -> None:
    """HTTP 服务返回固定规则版本、页面尺寸和模型原文，不夹带文件路径。"""
    output = BytesIO()
    Image.new('RGB', (300, 400), 'white').save(output, 'PNG')
    with TestClient(create_app(pipeline=ExternalPipeline())) as client:
        result = client.post('/parse', content=output.getvalue(), headers={'Content-Type': 'image/png'})
        assert result.status_code == 200, result.text
        body: dict[str, Any] = result.json()
        assert body['revision'] == 'pp-structure-v3-3.3.2-v1'
        assert body['width'] == 300 and body['height'] == 400 and body['angle'] == 0
        assert body['blocks'][0]['content'] == '原文 R-2048'
        assert body['blocks'][0]['bbox'] == [10, 20, 200, 60]
        assert 'input_path' not in result.text
        assert client.get('/health').status_code == 200
        assert client.post('/parse', content=b'not png').status_code == 415


def test_concurrent_page_waits_for_current_inference_instead_of_503() -> None:
    """模型繁忙时第二个页面应在有界时间内排队，不能因旧请求未结束而直接失败。"""
    entered, release = Event(), Event()
    output = BytesIO()
    Image.new('RGB', (300, 400), 'white').save(output, 'PNG')
    blob = output.getvalue()
    with TestClient(create_app(pipeline=BlockingPipeline(entered, release))) as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(client.post, '/parse', content=blob, headers={'Content-Type': 'image/png'})
            assert entered.wait(5)
            second = executor.submit(client.post, '/parse', content=blob, headers={'Content-Type': 'image/png'})
            release.set()
            assert first.result(timeout=10).status_code == 200
            assert second.result(timeout=10).status_code == 200
