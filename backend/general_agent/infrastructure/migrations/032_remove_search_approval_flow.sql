-- 联网搜索改为模型工具后不再存在外发确认；旧暂停尝试统一终止，允许用户重新发送。
UPDATE cloud_operations
SET cancelled = TRUE
WHERE id IN (
    SELECT operation_id
    FROM messages
    WHERE status = 'paused' AND operation_id IS NOT NULL
);

UPDATE search_requests
SET status = 'cancelled',
    error = '联网搜索已改为由模型直接调用，旧的外发确认已取消。'
WHERE operation_id IN (
    SELECT operation_id
    FROM messages
    WHERE status = 'paused' AND operation_id IS NOT NULL
);

UPDATE messages
SET status = 'failed',
    error = '联网搜索流程已更新，请重新发送问题。'
WHERE status = 'paused';
