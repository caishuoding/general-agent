ALTER TABLE memory_actions ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
CREATE TABLE memory_recalls (
    operation_id UUID PRIMARY KEY REFERENCES cloud_operations(id),
    message_id UUID NOT NULL REFERENCES messages(id),
    status TEXT NOT NULL CHECK(status IN ('pending','complete','failed','skipped','unavailable')),
    matches JSONB NOT NULL DEFAULT '[]',
    match_count INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    embedding_revision TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
