# PPT Master 无状态 Skill 验收记录

日期：2026-11-01。实现依据 `.scratch/skill-simplification/spec.md` 与 ADR 0022。上游固定于 `5e8856be0da8e6bc85c4aa28acc614478f7b7137`。

## 已验证行为

- 用户显式选择由前端读取完整 `SKILL.md` 并拼入用户消息；后端不重拼 description。
- 模型自主调用 `load_skill` 直接取得完整正文，不返回或保存 `task_id`。
- `read_skill_file` 仅允许读取会话已用 Skill 根目录内的相对文本；多 Skill 和跨消息集合均保留。
- 工作文件、模板和正式文件按会话持久化；命令和预览绑定当前 Run，生成文件用 `created_by_run_id` 记录来源。
- 删除会话会停止全部相关 Run 容器并删除整个会话生成目录。
- 文件工具不接收任务、账号、会话、Run 或绝对路径；后端通过当前 checkpoint 解析根目录。
- 生成文件不可变并重新核验账号、会话和 SHA-256。
- 真实 Docker 工具链完成写入、异步执行、轮询、SVG 检查、原生 PPTX 导出、发布和预览代理。
- 旧 `skill_tasks`、`runtime_state`、`instruction_files` 与 `skill_selections` 已从运行路径移除；测试迁移直接删除旧任务数据。

## 测试结果

- `TEST_SKILL_DOCKER=1 TEST_SKILL_BROWSER=1 pytest tests/skills -q`：11 passed。
- `mypy backend/general_agent`：通过。
- 前端 TypeScript 和 production build：通过。
- PPT Master attribution guard：通过。
- 完整 Python 套件多次运行到 75% 左右时本地测试 PostgreSQL 异常断开，后续错误为数据库断连级联；断连前的失败候选已单独复验通过，附件、文档、搜索和 Skill 模块分别通过。未将此次运行伪报为全绿。
- Standards/Spec 双轴审查已完成；审查发现由显式正文校验、预览 `answer_id`、提示词和类型注释问题已在提交前修正。

## 范围

未调用付费图片或语音服务；真实浏览器预览依赖当前环境中的 Node/Chromium，本次浏览器卡片测试通过，Docker 内 PPT 预览代理由测试接口验证。
