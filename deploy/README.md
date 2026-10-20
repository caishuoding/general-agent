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

可配置 DOCUMENT_CHUNK_CHARACTERS=1500、DOCUMENT_SEARCH_RESULTS=5。需要精确分词时，挂载对应模型官方 tokenizer.json，并向应用容器传入 MODEL_TOKENIZERS 的模型名到绝对路径 JSON 映射；没有对应文件时使用标记为估算的保守字符计数。分词运行库已锁定在应用 Python 依赖中。

到期访问在每个读取入口被拒绝；清理每分钟重试一次，远端图片清理失败也不会恢复文件访问。已完成聊天正文继续保留。没有使用私人文件调用外部模型验证文档理解质量。
