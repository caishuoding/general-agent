-- 联网搜索不再暂停确认，消息状态不再保留无法产生的 paused。
UPDATE messages
SET status = 'failed',
    error = COALESCE(error, '旧的暂停状态已取消，请重新发送问题。')
WHERE status = 'paused';

DROP INDEX IF EXISTS one_generating_answer;
ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_status_check;
ALTER TABLE messages ADD CONSTRAINT messages_status_check
    CHECK (status IN ('generating', 'complete', 'stopped', 'failed'));
CREATE UNIQUE INDEX one_generating_answer
    ON messages(conversation_id)
    WHERE status = 'generating';
