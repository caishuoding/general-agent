-- 新 AgentState 不解释旧检查点；保留业务消息、记忆、附件及生成文件。
UPDATE cloud_operations SET cancelled=TRUE
WHERE id IN (
    SELECT operation_id FROM messages WHERE status IN ('generating','interrupted')
    UNION
    SELECT operation_id FROM agent_runs WHERE status IN ('queued','running')
);
UPDATE messages SET status='failed',progress=NULL,
    error='执行结构已更新，请重试；已完成的操作和文件仍保留。'
WHERE status IN ('generating','interrupted');
UPDATE agents SET closed=TRUE WHERE NOT closed;
UPDATE agent_runs SET status='cancelled',paused_at=NULL,finished_at=CURRENT_TIMESTAMP
WHERE status IN ('queued','running');
-- 正在运行的 Skill 命令由启动恢复统一标为中断并停止容器。
DELETE FROM checkpoint_threads;
DROP SCHEMA IF EXISTS langgraph CASCADE;
