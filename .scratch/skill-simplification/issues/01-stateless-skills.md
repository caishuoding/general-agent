Status: ready-for-review

# 将 Skill 加载改为无状态并移除任务

## Comments

- 2026-11-01：前端显式拼接完整 `SKILL.md`，自主 `load_skill` 直接返回正文；工作文件、命令和预览改为绑定 `account_id + conversation_id + answer_id + run_id`。旧 `skill_tasks` 结构和测试数据直接删除。
