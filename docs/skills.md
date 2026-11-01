# PPT Master Skill 接入

上游固定版本见 `skills/ppt-master.lock.json`，完整发行目录位于 `skills/ppt-master/`，保持原样。运行镜像包含 PDF、Office、Excel、图像、旁白、视频和视觉检查依赖；外部服务仍需要真实配置。

Skill 是无状态说明包。加载 Skill 不创建账户任务、会话任务或第二套 Agent 上下文。

## 加载方式

### 用户显式选择

聊天输入区以紧凑名称卡片展示已安装 Skill。用户点击卡片后，前端调用 `GET /api/skills/<name>/content` 读取完整 `SKILL.md`，按选择顺序放入用户消息：

```text
【Skill：ppt-master】

<完整 SKILL.md>

【用户需求】

用户真实需求
```

发送请求同时附带技能名称数组。后端只校验名称并记录会话已使用 Skill，不再拼接 description 或保存描述快照。

### 模型自主加载

模型可调用 `load_skill(name)`。工具直接返回完整 `SKILL.md` 和容器内 Skill 根目录，不创建 task、目录或数据库状态。一个 Run 可以多次调用并加载多个 Skill。

`read_skill_file(skill, path)` 只允许读取当前会话已使用 Skill 的根目录内的相对文本文件。路径必须拒绝绝对路径、`..`、符号链接逃逸和目录。

## Agent State 与多 Skill

Agent State 的 `used_skills` 保存当前 Run 可访问的 Skill 名称列表。`conversation_skills` 保存会话使用过的全部名称，初始化后续 Run 的 `used_skills`。系统不会限制只能读取一个 Skill。

Skill 完整正文只保存在现有 user/tool messages 中；不设置 `runtime_state`、`instruction_files` 或 `set_skill_context`。

## 工作目录与工具

每个会话使用共享工作区；Run 只负责一次执行：

```text
APP_DATA_DIR/generated/<account_id>/<conversation_id>/
  workspace/
  libraries/
  artifacts/
  commands/
```

目录在第一次写文件或执行命令时按需创建。后续消息的新 Run 继续使用同一会话 workspace。模型工具只传相对路径，后端根据当前 `account_id + conversation_id` 解析会话目录，并用当前 `run_id` 复核执行身份。命令日志使用 `commands/<command_id>.log`。

主要工具：

- `load_skill`：返回完整 `SKILL.md`。
- `read_skill_file`：读取已用 Skill 的相对文本文件。
- `read_work_file`、`write_work_file`：读写当前会话共享 workspace。
- `run_skill_command`、`poll_skill_command`：异步执行和轮询隔离命令。
- `import_skill_source`：复制当前会话授权附件或知识库原件。
- `view_work_image`：让模型检查渲染后的 PNG/JPEG，不是聊天文件预览。
- `publish_skill_file`：保存不可变生成文件并返回下载地址。
- `open_skill_preview`：代理 PPT Master 自身的预览、标注或确认页面。

命令在独立 Docker 容器执行。容器只挂载当前会话的 workspace、已用 Skill 的会话模板副本和临时文件系统；不挂载应用配置、数据库或 Docker socket。出口仅允许通过公网代理访问标准 HTTP/HTTPS 端口。

## 命令限制

- 主执行加载 Skill 后最多 128 轮、384 次实际模型调用、一小时。
- 单条命令最多 900 秒。
- 全局最多三个并发命令，同一 Run 同一时间一个命令。
- 停止、超时或重启会中断命令，但保留 workspace 和生成文件。

## 文件与预览

生成文件直接绑定 `account_id + conversation_id`，并用 `created_by_run_id` 记录创建它的 Run；回答 ID 只保存在消息表。下载时重新核验账号、会话和 SHA-256。

删除整个会话时，后端先停止该会话全部 Run 的容器，再删除 `generated/<account_id>/<conversation_id>/` 下的 workspace、模板副本、命令日志和最终文件；数据库记录随会话外键级联清理。

`skill_previews` 保存一小时短期能力令牌。预览页在不透明源 CSP 沙箱运行，通过当前 Run 容器回环端口代理。

聊天界面显示生成文件下载入口；PPTX 缩略图或在线分页预览属于独立的普通文件预览能力，不等同于 `view_work_image` 或 `open_skill_preview`。

## 提供商配置

管理端“系统配置 → Skills 与媒体服务”按 `KEY=value` 配置图片、语音等服务变量。变量示例见 `deploy/skills/providers.env.example`。应用不使用聊天模型密钥；读取接口只返回变量名，文件保存权限为 `0600`。

## 部署

```sh
bash scripts/setup-skills.sh
export SKILL_HOST_DATA_DIR=/服务器上/general_agent/.local
docker compose -f compose.yaml -f compose.skills.yaml up -d --build app skill-egress
```

`SKILL_HOST_DATA_DIR` 必须指向 Docker 宿主机上的应用数据目录。任务容器不挂载 Docker socket；只有受信任应用后端持有 Docker 管理权限。

当前 schema 转换见 `068_simplify_skill_runtime.sql`。测试环境不保留旧的 `skill_tasks`、`skill_commands.task_id`、`skill_previews.task_id` 或 `messages.skill_selections` 数据。
