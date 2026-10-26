# 云服务器部署（Docker + Nginx）

当前部署目标：

- 公网 IP：`101.37.161.159`
- 用户端：`https://101.37.161.159/`
- 管理端：`https://101.37.161.159/admin`
- Nginx 监听 `443`，反向代理到 Docker 应用的 `127.0.0.1:8000`
- PostgreSQL、Mem0 和应用都由 Docker 运行
- 暂时没有域名，因此使用自签名证书；浏览器会提示证书不受信任，需要手动继续访问

## 首次部署

在服务器上安装 Docker、Docker Compose 和 Nginx：

```sh
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 nginx openssl rsync
```

把项目放到 `/opt/general-agent`，并把 `.local/model.json`、`.local/embedding.json`、`.local/search.json` 放到 `/opt/general-agent/.local/`（权限 600）。这些文件包含服务密钥，不要提交到 Git。

先启动 Mem0 网络和容器：

```sh
cd /opt/general-agent
docker compose -f mem0/docker-compose.yml up -d --build
```

再启动业务数据库和应用：

```sh
docker compose up -d --build
```

应用容器只绑定 `127.0.0.1:8000`；PostgreSQL、Mem0 和 Mem0 PostgreSQL 也只绑定本机回环地址。

## Nginx 443

生成自签名证书：

```sh
mkdir -p /etc/nginx/ssl
openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout /etc/nginx/ssl/general-agent.key \
  -out /etc/nginx/ssl/general-agent.crt \
  -subj "/CN=101.37.161.159" \
  -addext "subjectAltName=IP:101.37.161.159"
chmod 600 /etc/nginx/ssl/general-agent.key
```

安装配置并启动 Nginx：

```sh
cp deploy/nginx-general-agent.conf /etc/nginx/sites-available/general-agent
ln -sf /etc/nginx/sites-available/general-agent /etc/nginx/sites-enabled/general-agent
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx
```

云服务器安全组只需放行 `22`、`80`、`443`，不要放行 `8000`、`15432`、`8888`、`8432`。

## 首次管理员改密

应用在 Docker 内部，管理员的“仅服务器本机”判断需要使用容器内部请求完成。应用启动后执行：

```sh
cd /opt/general-agent
docker compose exec -T app python -c "import json,urllib.request; req=urllib.request.Request('http://127.0.0.1:8000/api/auth/login',data=json.dumps({'username':'admin','password':'123456','expected_role':'admin'}).encode(),headers={'Content-Type':'application/json'},method='POST'); r=urllib.request.urlopen(req); cookie=r.headers['Set-Cookie'].split(';')[0]; req2=urllib.request.Request('http://127.0.0.1:8000/api/auth/password',data=json.dumps({'current_password':'123456','new_password':'请替换成你的强密码'}).encode(),headers={'Content-Type':'application/json','Cookie':cookie},method='POST'); print(req2.read().decode())"
```

执行前把 `请替换成你的强密码` 换成至少 10 个字符的新密码。改密成功后，用 `admin` 和新密码登录 `https://101.37.161.159/admin`，再在管理端创建普通用户。

## 升级与备份

首次启用账号体系前，停止旧应用并执行：

```sh
uv run python scripts/auth_upgrade.py \
  --database-url "$DATABASE_URL" \
  --data-dir "$APP_DATA_DIR" \
  --mem0-url "$MEM0_API_URL" \
  --mem0-user-id "${MEM0_USER_ID:-local-person}"
```

脚本会先在应用数据目录的 `backups/auth-<时间戳>/` 生成：

- `database/*.csv`：每张业务表的 PostgreSQL COPY CSV 导出。
- `attachments.tar.gz`：附件原件归档。
- `manifest.json`：备份时间和各表行数。

备份成功后才会删除旧 Mem0 记忆、附件文件和业务表；Mem0 删除失败时不会清空业务数据。清空不可逆；请先确认 `manifest.json` 存在。

恢复时，先准备与备份结构兼容的数据库，再执行：

```sh
uv run python scripts/restore_auth_backup.py \
  --database-url "$RESTORE_DATABASE_URL" \
  --backup-dir "$APP_DATA_DIR/backups/auth-<时间戳>" \
  --data-dir "$RESTORE_DATA_DIR"
```

## 安全说明

- 没有域名时只能使用自签名证书，浏览器显示“不安全”提示；以后有域名后换成 Let's Encrypt 或云厂商证书即可。
- 初始管理员密码是已知引导凭据，必须在首次登录时修改；改密前应用拒绝公网登录。
- 本期没有登录失败次数限制或账号锁定。公网暴露后应监控登录日志，必要时在 Nginx 或云防火墙增加限流。
- `COOKIE_SECURE=true` 时，登录 Cookie 使用 `Secure`；Nginx 终止 HTTPS 后会把请求转发给应用。

## 文档工具与附件有效期

应用镜像通过 Dockerfile 安装 LibreOffice Writer 和 Noto CJK，Word 上传自动生成固定分页 PDF。原件和分页副本均保存在 APP_DATA_DIR/attachments，现有附件备份归档会一并覆盖。应用执行迁移 041 后在启动恢复中重建旧文字附件的原文及切片，不延长上传时计算的 30 天有效期。

模型管理配置增加 context_window_tokens（默认 32768）与 output_reserve_tokens（默认 4096），应填写实际模型窗口及回答预留。原文工具按剩余预算返回，单次不超过 10000 Token；本次不引入摘要压缩或滑动窗口。

可配置 DOCUMENT_CHUNK_TOKENS=800、DOCUMENT_CHUNK_OVERFLOW_TOKENS=200、DOCUMENT_CHUNK_OVERLAP_TOKENS=160、DOCUMENT_SEARCH_RESULTS=5；切片目标 800 Token，完整块可到 1000 Token，包含重叠和 Markdown 表头。overlap 为 0 时禁用，超过目标 20% 的配置自动收敛。旧 DOCUMENT_CHUNK_CHARACTERS／DOCUMENT_CHUNK_OVERLAP 配置不再用于预算。计数默认选择嵌入模型，可用 DOCUMENT_CHUNK_TOKENIZER_MODEL 覆盖；精确分词需挂载对应模型官方 tokenizer.json，并传入 MODEL_TOKENIZERS 的模型名到绝对路径 JSON 映射。没有对应文件时按 UTF-8 字节数保守估算，元数据标记 character_estimate。迁移 045 保存结构包装及计数信息，启动按解析与计数配置指纹重建 Word，不延长 TTL。分词运行库已锁定在应用 Python 依赖中。

到期访问在每个读取入口被拒绝；清理每分钟重试一次，远端图片清理失败也不会恢复文件访问。已完成聊天正文继续保留。没有使用私人文件调用外部模型验证文档理解质量。


### 附件混合召回配置

目标行为见 [ADR 0013](../docs/adr/0013-attachment-hybrid-retrieval.md) 及 [实施规格](../.scratch/attachment-hybrid-retrieval/spec.md)，当前进度以规格 Completion 为准。复用 APP_DATA_DIR/embedding.json 的嵌入配置，不单独配置附件模型。新增迁移 043 保存按嵌入配置版本区分的切片向量，旧附件首次查询时补齐，不刷新 TTL；缓存随切片删除。

智谱重排使用 ZHIPU_RERANK_API_KEY 环境变量或 APP_DATA_DIR/rerank.json 私有文件（字段 api_key），环境变量优先；私有文件权限为 0600。默认地址为 https://open.bigmodel.cn/api/paas/v4/rerank，超时 10 秒。密钥不得写入版本库、日志或工具结果。重排未配置或失败时仍提供融合结果，embedding 失败时仍提供 BM25 结果；通过工具结果的 retrieval 字段识别降级。现阶段不添加新管理界面。

智谱协议依据：[官方 OpenAPI](https://docs.bigmodel.cn/openapi/openapi.json) 的 `/paas/v4/rerank`：模型 `rerank`，最多 128 条候选，每条最多 4096 字符。超长候选不截断正文，明确标记 `rerank_input_too_long` 并使用融合顺序。


迁移 044 增加 Word 原生结构元数据。Word 正文与数字直接来自原件，固定分页副本通过不可见书签定位原生段落和表格行，不再作为正文提取来源；跨页块保持完整并返回起止页。启动恢复按解析版本与切片配置升级 Word 和 PDF 索引，原 TTL 不变。表格转标准 Markdown，按完整行分组，各切片重复表头并计入预算，数据行不做 overlap，超长单行按单元格续片；章节之间不重叠。

文字型 PDF 新增 pdfplumber 坐标与线条表格解析，依赖已锁定在 uv.lock 和 requirements-server.txt。解析版本 pdf_tables_v2 首次启动时从有效 PDF 原件重建索引，旧向量随切片撤销，之后配置和版本匹配时不会重复重建；上线前保留业务数据库及原件备份。有框与保守识别的无框表格转 Markdown，表格按行拆片并重复表头；单页表格识别失败回退纯文字并标记缺口。原有 20 秒、内存、200 页和字符资源上限继续生效。

迁移 048 增加 document_images：Word／PDF 的常见栅格图片保存稳定 image_id、页码／段落、结构化描述和 DeepSeek 文件身份；图片描述切片与普通文字共用检索。当前使用管理端配置的 deepseek-flash，解析完成后异步调用视觉模型；图片失败只标记该图片。召回后同一回答的每张图片只发送一次，单批最多五张。派生文件位于 APP_DATA_DIR/attachments/<UUID>.images，删除和到期时与远端文件一起清理。第一版不处理 SmartArt、EMF、WMF、矢量图表及 Markdown 外部图片引用，每份文档最多处理 50 张图片。

## 扫描 PDF OCR

扫描 PDF 需要额外部署本地 PP-StructureV3 服务。使用 `docker compose -f compose.yaml -f compose.ocr.yaml up -d --build` 叠加 OCR；业务应用通过 `DOCUMENT_OCR_URL` 访问。模型缓存独立保存，文字解析与 OCR 推理有各自资源预算。首次启动会下载官方模型，模型健康检查通过后应用才启动。运行时版本、坐标协议、资源与验证说明见[OCR 运行时](../ocr/README.md)及[ADR 0018](../docs/adr/0018-scanned-pdf-layout-ocr.md)。
