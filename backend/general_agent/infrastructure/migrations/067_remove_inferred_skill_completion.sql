-- 后端不再推断制作任务完成；旧 completed 统一回到可继续的 waiting。
ALTER TABLE skill_tasks DROP CONSTRAINT IF EXISTS skill_tasks_status_check;
UPDATE skill_tasks SET status='waiting',updated_at=CURRENT_TIMESTAMP WHERE status='completed';
ALTER TABLE skill_tasks
ADD CONSTRAINT skill_tasks_status_check
CHECK(status IN ('active','waiting','interrupted','revoked'));
