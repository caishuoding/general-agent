Status: ready-for-agent

# 简化 Skill 加载与按 Run 隔离工作区

## 合同

- Skill 是静态目录，不创建 `skill_task`，不绑定账户或会话。
- 用户显式选择时，前端读取完整 `SKILL.md` 并放入用户消息；发送请求只附带技能名称元数据。
- 模型自主选择时，`load_skill(name)` 返回完整 `SKILL.md`，不创建 task、目录或 task 状态；只更新会话已用 Skill 名称索引。
- Agent State 使用 `used_skills` 保存当前 Run 可访问的 Skill 名称；`conversation_skills` 保存会话级使用过的 Skill 名称，支持多 Skill 和跨消息访问。
- 生成文件绑定 `account_id + conversation_id` 并用 `created_by_run_id` 记录来源；命令和预览绑定当前 `run_id`；回答 ID 只保存在消息表。
- 会话目录为 `generated/<account_id>/<conversation_id>/{workspace,libraries,artifacts,commands}`，后续消息的新 Run 共用同一 workspace。
- 工具只接收相对路径；后端按会话解析根目录、按当前 Run 复核权限，并拒绝路径逃逸。
- 用户消息允许容纳最多 2 MB 的完整 Skill 正文，超限由现有模型上下文预算明确拒绝。
- `read_skill_file` 只读取 `used_skills` 中对应 Skill 根目录内的文件。
- 删除 `skill_tasks`、`task_id`、`runtime_state`、`instruction_files`、描述快照和自动恢复旧 Skill 工作流。
- 测试环境允许清空旧数据，不实现兼容迁移。

## 验证缝

1. Skill 内容 API：返回完整 `SKILL.md`，只接受已安装 Skill 名称。
2. Agent 工具边界：显式选择和自主加载均无需 task；支持多个 Skill；相对路径受根目录限制。
3. 生成文件与预览 API：按用户、会话和 Run 授权，不依赖 task。
