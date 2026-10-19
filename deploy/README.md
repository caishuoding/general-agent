# 公网部署

## 前提

- 云服务器已安装 Docker、uv、Node.js 和 Caddy。
- 服务器公网 IP 为 `203.0.113.5` 时，临时主机名是 `203.0.113.5.sslip.io`。`sslip.io` 会把该名称解析回服务器 IP，不需要购买域名。
- 业务 PostgreSQL、Mem0 和应用按项目 README 启动。

## 首次初始化

1. 先只启动应用，暂不启动 Caddy。应用配置：

   ```sh
   PUBLIC_HOST=203.0.113.5.sslip.io
   DATABASE_URL=postgresql://general_agent:...@127.0.0.1:15432/general_agent
   APP_DATA_DIR=/srv/general-agent/data
   ```

   不要在 SSH 引导阶段设置 `COOKIE_SECURE=true`；应用会根据 HTTPS 反向代理自动给公网 Cookie 加 `Secure`。

2. 在本地电脑建立 SSH 隧道：

   ```sh
   ssh -L 8000:127.0.0.1:8000 user@203.0.113.5
   ```

   浏览器打开 `http://127.0.0.1:8000`，使用 `admin / 123456` 登录。首次登录会强制设置新密码；完成后再关闭隧道。

3. 启动 Caddy：

   ```sh
   PUBLIC_HOST=203.0.113.5.sslip.io caddy run --config deploy/Caddyfile
   ```

   浏览器打开 `https://203.0.113.5.sslip.io`。Caddy 自动申请和续期证书，并反向代理到 `127.0.0.1:8000`。

4. 防火墙只开放 `80` 和 `443`，不要开放 `8000`、`15432` 或 Mem0 端口。

## 升级与备份

首次启用账号体系前，停止旧应用并执行：

```sh
uv run python scripts/auth_upgrade.py \
  --database-url "$DATABASE_URL" \
  --data-dir "$APP_DATA_DIR"
```

脚本会先在应用数据目录的 `backups/auth-<时间戳>/` 生成：

- `database/*.csv`：每张业务表的 PostgreSQL COPY CSV 导出。
- `attachments.tar.gz`：附件原件归档。
- `manifest.json`：备份时间和各表行数。

备份成功后才会清空旧业务数据。清空不可逆；请先确认 `manifest.json` 存在。随后启动新应用，数据库迁移会创建账号、会话和审计表，并预置 `admin / 123456`。

恢复时，先准备与备份相同结构的数据库，再执行：

```sh
uv run python scripts/restore_auth_backup.py \
  --database-url "$RESTORE_DATABASE_URL" \
  --backup-dir "$APP_DATA_DIR/backups/auth-<时间戳>" \
  --data-dir "$RESTORE_DATA_DIR"
```

恢复脚本按 CSV 表头写入同名表和列，并解压附件归档。结构已经变化时，不能直接把旧 CSV 导入新表；应先恢复旧结构或手工转换。

## 安全说明

- 初始管理员密码是已知引导凭据，必须在首次登录时修改；改密前应用拒绝公网登录。
- 本期没有登录失败次数限制或账号锁定。公网暴露后应监控登录日志，必要时在 Caddy 或云防火墙增加限流。
- `COOKIE_SECURE` 可用于强制 Cookie 安全属性；反向代理提供 `X-Forwarded-Proto: https` 时，应用也会自动启用 `Secure`。
