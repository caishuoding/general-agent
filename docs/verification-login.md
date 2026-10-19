# 登录与管理员后台验收记录

日期：2026-10-19

## 范围

本次实现覆盖预创建多账号登录、首次强制改密、7 天服务端会话、账号隔离、管理员后台、跨账号只读查看与审计、旧数据备份清空、Caddy 公网 HTTPS 配置和部署文档。架构决策见 [ADR 0010](adr/0010-precreated-accounts-and-admin-console.md)，规格见[登录与后台账号管理规格](../.scratch/login/spec.md)。

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| Python 完整测试 `uv run pytest -q` | 101 passed |
| Python 类型检查 `uv run mypy` | 通过，98 个源文件 |
| 前端类型检查 `pnpm typecheck` | 通过 |
| 前端构建 `pnpm build` | 通过 |
| Playwright 主流程 | 11 passed |
| Playwright 图片 | 1 passed |
| Playwright 联网搜索 | 1 passed |
| Playwright 来源删除 | 1 passed |

重点覆盖：

- `admin / 123456` 首次登录必须改密，改密前公网来源登录被拒绝。
- 登录、退出、会话过期、停用账号、改密后旧会话失效、管理员重置密码。
- HTTPS 登录 Cookie 包含 `HttpOnly`、`Secure`、`SameSite=Lax`。
- 普通用户不能读取他人会话、附件或管理员配置。
- 管理员可以创建、停用、启用和重置账号，最后一个有效管理员不能被停用或降级。
- 管理员可以只读查看目标账号的会话、消息、附件、记忆、搜索和用量，并产生审计记录。
- 旧数据升级会先备份 CSV 与附件归档，备份失败时不清空；清空后新迁移可创建账号结构并预置管理员。
- 登录页、强制改密页、普通用户隐藏管理入口、管理员后台和退出流程。

## 待验事项

- 尚未在真实云服务器上执行 Caddy 的 `IP.sslip.io` 证书申请和浏览器 HTTPS 检查；配置文件与步骤已完成，需在部署环境实测。
- 尚未对用户现有真实数据库执行升级清空；这会删除现有业务数据，必须在执行前核对备份并再次确认。
- 备份恢复脚本已提供，但未在真实旧数据卷上做完整恢复演练；正式升级前应使用独立数据库验证一次。
- 本期按用户决定不实现登录失败次数限制或账号锁定，公网环境仍存在暴力尝试风险。

## 复现命令

```sh
uv run pytest -q
uv run mypy
pnpm --dir frontend typecheck
pnpm --dir frontend build
pnpm --dir frontend test:e2e
```
