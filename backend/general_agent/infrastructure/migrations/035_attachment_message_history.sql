-- 记录附件首次随哪条用户消息发送，仅用于聊天记录展示；回答上下文仍读取整个会话。
ALTER TABLE attachments
ADD COLUMN IF NOT EXISTS user_message_id UUID REFERENCES messages(id) ON DELETE SET NULL;

-- 已有附件回填到上传之后的第一条用户消息，避免升级后历史记录仍看不到附件。
UPDATE attachments AS attachment
SET user_message_id = (
    SELECT message.id
    FROM messages AS message
    WHERE message.conversation_id = attachment.conversation_id
      AND message.role = 'user'
      AND message.created_at >= attachment.created_at
    ORDER BY message.position
    LIMIT 1
)
WHERE attachment.user_message_id IS NULL;

CREATE INDEX IF NOT EXISTS attachments_user_message ON attachments(user_message_id);
