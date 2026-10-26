"""常驻 PP-StructureV3 的私网 HTTP 边界，逐页输出有序结构并保留可逆旋转。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any
import os
import warnings

from fastapi import FastAPI, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

REVISION = 'pp-structure-v3-3.3.2-v1'
MAX_UPLOAD = 20_000_000
MAX_PIXELS = 40_000_000


def create_app(pipeline: Any = None, orientation: Any = None) -> FastAPI:
    """建立串行推理服务；模型仅在服务启动时加载，测试可替换第三方模型 SDK。"""
    lock = Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """加载固定版本 OCR 和方向模型，避免每页重复加载或规则静默变化。"""
        nonlocal pipeline, orientation
        if pipeline is None:
            if version('paddleocr') != '3.3.2' or version('paddlex') != '3.3.13':
                raise RuntimeError('OCR 运行库版本与协议不匹配。')
            sdk = import_module('paddleocr')
            device = os.environ.get('OCR_DEVICE', 'cpu')
            orientation = sdk.DocImgOrientationClassification(device=device)
            pipeline = sdk.PPStructureV3(
                device=device, use_doc_orientation_classify=False, use_doc_unwarping=False,
                use_textline_orientation=True, use_table_recognition=True,
                use_formula_recognition=False, use_chart_recognition=False, use_seal_recognition=False,
                layout_detection_model_name=os.environ.get('OCR_LAYOUT_MODEL', 'PP-DocLayout-S'),
                text_detection_model_name=os.environ.get('OCR_TEXT_DETECTION_MODEL', 'PP-OCRv5_mobile_det'),
                text_recognition_model_name=os.environ.get('OCR_TEXT_RECOGNITION_MODEL', 'PP-OCRv5_mobile_rec'),
                # PaddleX 3.3.13 仍初始化已关闭的图表槽位；用已缓存的轻量方向模型占位。
                # use_chart_recognition=False 确保该槽位不参与推理，插图继续由业务视觉模型描述。
                chart_recognition_model_name='PP-LCNet_x1_0_doc_ori',
            )
        yield

    app = FastAPI(lifespan=lifespan)

    def recognize(blob: bytes) -> dict[str, Any]:
        """校验 PNG 后先作整页正交旋转，再识别相同画布，模型忙时明确拒绝排队。"""
        if not lock.acquire(timeout=300):
            raise HTTPException(503, 'OCR 正在处理其他页面，请稍后重试。')
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(blob)) as source:
                    if source.format != 'PNG' or source.width * source.height > MAX_PIXELS:
                        raise HTTPException(413, '只支持不超过四千万像素的 PNG 页面。')
                    source.load()
                    image = source.convert('RGB')
            try:
                with TemporaryDirectory(prefix='pdf-ocr-') as temporary:
                    path = Path(temporary) / 'page.png'
                    image.save(path, 'PNG')
                    angle = 0
                    if orientation is not None:
                        prediction = next(iter(orientation.predict(str(path))))
                        angle = int(prediction['label_names'][0])
                        if angle not in (0, 90, 180, 270):
                            raise ValueError('不支持非正交旋转。')
                    with image.rotate(angle, expand=True) as normalized:
                        normalized.save(path, 'PNG')
                        width, height = normalized.size
                    result = next(iter(pipeline.predict(str(path))))
                    native = result.json['res']
                    blocks = [
                        {'label': block['block_label'], 'bbox': block['block_bbox'],
                         'content': block.get('block_content') or '', 'confidence': block.get('confidence')}
                        for block in native['parsing_res_list']
                    ]
                    scores = native.get('overall_ocr_res', {}).get('rec_scores', [])
                    return {'revision': REVISION, 'angle': angle, 'width': width, 'height': height,
                            'confidence': sum(scores) / len(scores) if scores else None, 'blocks': blocks}
            finally:
                image.close()
        except HTTPException:
            raise
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise HTTPException(422, '页面图片损坏或超过资源限制。') from None
        except Exception:
            raise HTTPException(503, '扫描页版面识别未完成，请重试。') from None
        finally:
            lock.release()

    @app.get('/health')
    def health() -> dict[str, str]:
        """仅在模型加载完成后报告服务就绪及当前规则版本。"""
        return {'status': 'ready', 'revision': REVISION}

    @app.post('/parse')
    async def parse(request: Request) -> dict[str, Any]:
        """以流式有界方式读取页面，推理在线程执行，HTTP 事件循环不被阻塞。"""
        if request.headers.get('content-type', '').split(';')[0] != 'image/png':
            raise HTTPException(415, '请发送 image/png 页面。')
        blob = bytearray()
        async for chunk in request.stream():
            blob.extend(chunk)
            if len(blob) > MAX_UPLOAD:
                raise HTTPException(413, '页面图片超过 20 MB。')
        result: dict[str, Any] = await run_in_threadpool(recognize, bytes(blob))
        return result

    return app
