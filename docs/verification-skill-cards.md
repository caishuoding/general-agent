# Skill 卡片显式选择验收记录

日期：2026-11-01。实现依据 `.scratch/skill-simplification/spec.md` 与 ADR 0022。

## 已验证行为

- 卡片只显示真实 Skill 名称，支持多选、取消和清除。
- 用户点击卡片后不提前创建任务；发送时前端读取每个所选 Skill 的完整 `SKILL.md`，按选择顺序拼入用户消息。
- 请求同时提交技能名称元数据；后端只校验名称并写入会话已用 Skill，不再拼接 description 或保存描述快照。
- 历史消息显示完整用户消息与轻量 Skill 名称标签，重试直接沿用原内容。
- 发送成功清空选择和草稿；失败恢复原选择与草稿，迟到失败不覆盖用户后来输入。
- 手机、键盘、输入法、触摸和横向溢出行为保持可用。

## 自动验证

- `pnpm --dir frontend exec playwright test skill-cards.spec.ts`：3 项通过。
- `pnpm --dir frontend exec tsc --noEmit`：通过。
- `pnpm --dir frontend build`：通过。
- `pytest tests/skills/test_message_selection.py`：通过，验证完整正文只出现一次、名称去重、未知名称拒绝。
- `pytest tests/skills/test_simplified_skill_content.py`：通过，验证完整入口接口。
