# 完整 PPT Master 接入

上游固定版本见 `skills/ppt-master.lock.json`，完整发行目录位于 `skills/ppt-master/`，保持原样；本项目新增代码遵循中文函数注释规范，上游文件保留其原有注释及 MIT/资源归属。镜像包含 PDF、Office、Excel、图像、旁白、视频和视觉检查依赖，外部服务仍需要真实配置。

Python 运行依赖固定在 `deploy/skills/requirements.lock`，来自已实际构建和验收的完整镜像；构建运行 `pip check`、系统 Chromium 截图和上游归属完整性检查。升级上游时需重新解析、验证并更新该锁文件。

## 准备运行环境

本机已经启动 Docker 后执行：

```sh
bash scripts/setup-skills.sh
```

本机直接启动后端时，沿用 `scripts/start.sh`。Docker 部署使用：

```sh
export SKILL_HOST_DATA_DIR=/服务器上/general_agent/.local
docker compose -f compose.yaml -f compose.skills.yaml up -d --build app skill-egress
```

先执行 setup 构建 Skill 镜像，再升级应用。`SKILL_HOST_DATA_DIR` 必须指向 Docker 宿主机实际挂载的应用数据目录，不能填应用容器内的 `/app/.local`。本机开发默认使用实际 `APP_DATA_DIR`。Docker Desktop 自定义 socket 时配置 `DOCKER_SOCKET_PATH`。只在受信任应用后端挂载 Docker socket，任务容器不挂载它、不挂载应用配置、不连接业务网络。

默认镜像 `general-agent-skills:5e8856be`、内部网络 `general-agent-skills`。任务只能经 `skill-egress` 代理连接公网 HTTP/HTTPS 标准端口；代理拒绝私网、回环和元数据地址。自建提供商需要公网 HTTPS 地址。请求、抓取与服务费用按实际提供商计费；当前应用用量页只统计现有调用网关，Skill 脚本直接调用的图片/语音服务不伪装成已计量。

本机代理若使用 198.18.0.0/15 等 Fake-IP DNS，出口仍会拒绝这些保留地址。可显式设置 `SKILL_EGRESS_DOH=https://cloudflare-dns.com/dns-query` 或 `https://dns.google/resolve` 后重建出口容器；它通过可信 HTTPS 解析取得真实公网 IP，再校验并固定连接目标，不放宽私网限制。

## 用户流程

在会话上传报告、PPTX 模板或 Excel 等资料，直接要求使用 PPT Master。agent 看到所有已安装 Skill 的完整 description，自主调用 `load_skill`，再按入口读取对应工作流和引用文件。普通聊天只提供目录与制作任务摘要；用户明确继续时通过 task_id 续接原任务。

聊天输入区上方以紧凑名称卡片展示已安装 Skill，不显示简介、版本或展开说明。点击卡片可显式选择一个或多个技能，输入框只保存用户真实需求；选择不会单独发送消息或创建制作任务。发送时后端读取已安装目录并将完整 description 与真实需求拼成一条 user message，用户消息正文仍保存原文，同时保存名称、版本和描述快照。历史消息以技能标签展示，展开可核对当时使用的描述；失败重试和后续模型上下文继续使用该快照，不会随技能升级静默替换。

入口、活动 Markdown 指令每次模型请求都由可信加载器重新组装，正文完整且计入模型上下文预算。需要为所用模型配置真实窗口与分词器；空间不足会明确失败，不能静默截掉工作流。上游入口指令、模型生成的大量 SVG 和参考规范可能需要较大的窗口。

新增指令先预检完整请求预算，再保存活动集合；跨消息续接保留阶段和历史已读路径，从完整入口重新按当前阶段加载，避免上次超预算的参考集合阻塞恢复。`set_skill_context` 可显式卸载不再适用的阶段文档，入口始终保留。

每个任务有独立工作目录。命令异步执行，agent 轮询真实状态并读取有界日志；上游要求确认时结束当前回答，由后端自动转为 waiting，下一条用户消息恢复原任务及有效指令。普通回答保持既有预算；激活制作任务后主执行最多 128 轮、384 次实际模型调用、一小时，单条命令最多 900 秒、最多三个并发命令。工作文件与任务状态自动持久化，不再向模型提供 save_skill_task。上下文按预算自动压缩，重启后可点击“继续执行”；达到执行上限不能声称已经完成。

用户可下载真实发布的不可变产物。会话下方显示制作状态与文件。上游预览使用 `--no-browser`，启动后调用 `open_skill_preview`；应用提供一小时短期授权地址并代理到任务容器回环端口。预览脚本在不透明源沙箱中运行，不能读取主应用会话；标注和直接编辑仍由原生预览器保存，PPT 重新导出由聊天 agent 执行。

默认方案确认页也通过同一受保护代理打开：`open_skill_preview(surface="confirmation")` 读取 `.confirm_ui.lock`；页面提交后用户回到会话要求继续，agent 读取真实回执。模板注册使用每个任务的独立模板库根，挂到对应 Skill 的 `templates/`，不改写安装目录；任务文件读写使用目录描述符和 O_NOFOLLOW，禁止符号链接竞态越界。

## 提供商配置

管理端“系统配置 → Skills 与媒体服务”按 `KEY=value` 配置。变量示例在 `deploy/skills/providers.env.example`；按选用的图片、语音提供商填写，应用不自动使用聊天密钥。保存后只展示配置过的变量名，密钥不回显；空值保留旧值，清除按钮显式删除全部配置。文件位于 `APP_DATA_DIR/skill-providers.json`，权限 0600；备份时作为凭据保管。

默认所有上游能力文件完整保留。需要外部 API、特定宿主或额外原生软件的流程按实际条件执行；例如上游标注 Codex 专用的图像还原流程，当前自建 agent 不能冒充 Codex，入口应如实说明宿主限制。

## 停止、恢复和来源

用户停止回答、执行到期或资料范围变化会中断命令；应用重启终止旧命令，保留任务文件供用户明确续接，不恢复已经中断的模型网络请求。后台命令日志和任务元数据不等于模型私有推理。当前以完整资料清单快照保守授权：附件或所选知识库范围变化后旧制作任务撤销并清理复制资料，用户需创建新任务。

`APP_DATA_DIR/skill-tasks/<task_id>/` 保存工作文件、命令日志及不可变交付副本；任务元数据随会话保存在业务 PostgreSQL。会话删除或来源失效后不得再访问，维护流程清理容器及孤立目录。升级 Skill 必须同时升级完整文件和运行镜像；旧任务摘要与安装版本不一致时拒绝静默续接。
