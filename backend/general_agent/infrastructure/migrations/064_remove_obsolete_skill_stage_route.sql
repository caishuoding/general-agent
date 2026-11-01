-- 后端已按真实工具执行和回答终态更新任务状态。
-- 旧 save_skill_task 留下的自由文本阶段与路由没有读取或写入方。
ALTER TABLE skill_tasks DROP COLUMN IF EXISTS stage;
ALTER TABLE skill_tasks DROP COLUMN IF EXISTS route;
