-- 历史检索只保存真实消息标识与指纹，不另存原文或摘录。
CREATE TABLE history_queries (
    operation_id UUID PRIMARY KEY REFERENCES cloud_operations(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    request_message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    result_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE history_hits (
    operation_id UUID NOT NULL REFERENCES history_queries(operation_id) ON DELETE CASCADE,
    source_message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    source_hash TEXT NOT NULL,
    source_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(operation_id,source_message_id)
);
CREATE INDEX history_hits_source_conversation ON history_hits(source_conversation_id);
CREATE INDEX history_hits_source_message ON history_hits(source_message_id);
