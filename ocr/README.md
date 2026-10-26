# 扫描 PDF OCR 运行时

扫描 PDF 使用 PaddleOCR **3.3.2** 的 PP-StructureV3，配合 PaddleX **3.3.13** 与 PaddlePaddle **3.2.2**。`requirements.txt` 声明顶层版本，`requirements.lock.txt` 固定完整运行时依赖；业务应用不安装这些模型依赖。

## 与业务应用的关系

1. 业务应用在受限子进程检查原 PDF、识别扫描页，并逐页渲染 PNG。
2. 对每张页面图片调用本服务 `/parse`。服务先判断正交方向，用同一旋转画布识别正文、表格和插图区域，返回有序内容块。
3. 应用将文字/表格转换成 DocumentUnit，裁剪插图并绑定图注，再调用现有视觉模型描述。
4. 全部中间内容准备完成后，复用现有切片、索引、检索和引用。整页扫描图不是图片描述对象；原始 PDF 是核对依据。

OCR HTTP 请求仅包含页面 PNG，不携带聊天模型密钥。`/parse` 返回固定版本 `pp-structure-v3-3.3.2-v1`、正交旋转角度（逆时针）、识别画布像素尺寸、按阅读顺序排列的 blocks 和可选置信度。应用逆映射到原始 PDF 的 point 坐标，裁剪使用相同旋转画布。非线性展平保持关闭。PaddleX 3.3.13 会无条件初始化已关闭的图表槽位；服务将该槽位配置为同一个已缓存的轻量方向模型占位，图表推理始终关闭，避免下载和加载未使用的图表模型。

## Docker 部署

在原有 Compose 上叠加本仓库的 OCR 文件：

```sh
docker compose -f compose.yaml -f compose.ocr.yaml up -d --build
```

该配置为应用设置 `DOCUMENT_OCR_URL=http://ocr:8090`，OCR 不发布宿主端口。模型保存在 `ocr_models` 数据卷，首次启动需联网下载官方模型；模型加载完毕 `/health` 才报告就绪。默认 CPU、6 GiB 容器内存上限、2 CPU 配额，实际吞吐与内存需在目标机器验收；资源不足时可先保持原部署，另行部署 OCR 服务后设置连接地址。

不要删除现有数据库、Mem0 或原件数据卷。上线前备份业务数据库和原件；本轮代码更新后的 PDF 规则版本将触发既有索引恢复，人工清空标记和附件原 TTL 保留。OCR 服务不可用会导致扫描页缺口；普通文字页继续按原生方式解析。

## 本机运行

Python 3.12 环境示例：

```sh
uv venv .local/ocr-env --python 3.12
uv pip install --python .local/ocr-env/bin/python -r ocr/requirements.lock.txt
PADDLE_PDX_CACHE_HOME="$PWD/.local/ocr-models" \
  .local/ocr-env/bin/uvicorn ocr.server:create_app --factory --host 127.0.0.1 --port 8090 --limit-concurrency 4
```

另一个终端启动应用前设置：

```sh
export DOCUMENT_OCR_URL=http://127.0.0.1:8090
```

`OCR_DEVICE` 默认 `cpu`。GPU 部署须使用目标硬件匹配的 Paddle GPU 运行库；此 CPU 锁文件不宣称涵盖 CUDA 兼容配置。

## 限制与失败处理

- 模型常驻并串行推理；并发页面最多等待 300 秒后依次执行，等待超限才返回 503，避免旧请求尚未结束时把后续页误判为失败。
- 单张 PNG 最多 20 MB、40,000,000 像素；应用单页 OCR 请求最长 300 秒，整份 OCR 最长 60 分钟，最多 200 页。
- 原始文档检查及每次渲染仍有 20 秒、512 MiB 监控和工作进程 CPU 限制。
- 单次 OCR 响应最多 16 MB；文档总正文最多 2,000,000 字符、20,000 个结构块；独立插图及表格截图合计最多 50 张。
- 表格以模型显式表头和单元格跨度为依据，不能确认表头时使用纯文字和本地截图，不把猜测结构当作原文。
- 无可靠表头的长表纯文字仍按 Token 预算切片，每片保留同一截图身份及实际本片文字。
- 表格未提供可靠行级坐标时，行引用保留整张表格的真实区域，不伪造单行精确位置。旋转页暂不依据原页边缘合并续表。
- 页级置信度低于 0.8 时标记需核对，保留可读文字；此阈值不是准确率承诺。唯一页边备注保留，重复确认的页眉页脚才清理。
- 第一版中间结果只在内存中传递，失败重试重新 OCR；已有图片描述及向量仍按原规则复用。未提供持久化 OCR 缓存。

## 验证

受控协议测试不需要下载模型。真实模型单独验收：

```sh
OCR_ACCEPTANCE_URL=http://127.0.0.1:8090 .venv/bin/pytest -q \
  tests/attachments/test_pdf_ocr.py::test_real_structure_ocr_reads_synthetic_scan
```

该测试生成合成印刷体扫描件，不调用外部聊天模型或使用私人资料。实际中文、表格、插图及低清扫描件仍应以用户样本核对，具体执行结果见 `docs/verification-pdf-scan-ocr.md`。
