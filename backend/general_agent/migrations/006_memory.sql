CREATE TABLE memory_actions (
    id UUID PRIMARY KEY,
    message_id UUID NOT NULL REFERENCES messages(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    operation_id UUID REFERENCES cloud_operations(id),
    fact TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','saving','complete','failed')),
    memory_id UUID UNIQUE,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(message_id)
);
CREATE TABLE mem0_history (
    seq BIGSERIAL PRIMARY KEY,
    id TEXT UNIQUE NOT NULL,
    memory_id TEXT NOT NULL,
    old_memory TEXT,
    new_memory TEXT,
    event TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    actor_id TEXT,
    role TEXT
);
CREATE INDEX mem0_history_memory ON mem0_history(memory_id);
ALTER TABLE messages ADD COLUMN operation_id UUID UNIQUE REFERENCES cloud_operations(id);
ALTER TABLE messages ADD COLUMN model_revision TEXT;
ALTER TABLE messages ADD COLUMN embedding_revision TEXT;
ALTER TABLE messages ADD COLUMN budget_pause JSONB;
ALTER TABLE messages DROP CONSTRAINT messages_status_check;
ALTER TABLE messages ADD CONSTRAINT messages_status_check CHECK(status IN ('generating','paused','complete','stopped','failed'));
DROP INDEX one_generating_answer;
CREATE UNIQUE INDEX one_generating_answer ON messages(conversation_id) WHERE status IN ('generating','paused');
