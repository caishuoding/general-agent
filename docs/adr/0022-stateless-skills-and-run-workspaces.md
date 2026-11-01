# 无状态 Skill 与会话工作区

Status: accepted

Skill 是管理员安装的静态说明包。用户显式选择时，前端读取完整 `SKILL.md` 并放入用户消息；模型自主选择时，`load_skill` 只返回完整正文。两种入口都不创建 Skill 任务或目录；`load_skill` 仅更新会话已用 Skill 名称索引。

多 Skill 通过 Agent State 的 `used_skills` 和会话级 `conversation_skills` 名称集合表达。名称只用于允许读取对应 Skill 根目录；完整正文仍保存在现有 user/tool messages 中，不保存 description 快照或 `instruction_files`。

Workspace、模板副本和正式文件按 `account_id + conversation_id` 持久化，使后续消息的新 Run 能继续编辑同一项目。命令和预览绑定当前 `run_id`，生成文件使用 `created_by_run_id` 记录来源；回答 ID 只保存在消息表。目录为 `generated/<account_id>/<conversation_id>/{workspace,libraries,artifacts,commands}`，按首次操作创建。工具只接收相对路径，后端解析会话根目录并拒绝路径逃逸。

本决策取代 ADR 0020 中制作任务、固定阶段、`task_id` 续接和来源快照撤销方案。测试环境允许废弃旧数据，迁移直接删除旧 Skill 任务结构，不保留兼容双写。
