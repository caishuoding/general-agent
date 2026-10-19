-- 删除跨会话历史聊天检索；原聊天消息仍保留在 messages 表。
DROP TABLE IF EXISTS history_hits;
DROP TABLE IF EXISTS history_queries;
