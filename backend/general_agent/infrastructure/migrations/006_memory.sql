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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX memory_actions_source_message ON memory_actions(message_id);
ALTER TABLE messages ADD COLUMN operation_id UUID UNIQUE REFERENCES cloud_operations(id);
ALTER TABLE messages ADD COLUMN model_revision TEXT;
ALTER TABLE messages ADD COLUMN embedding_revision TEXT;
ALTER TABLE messages DROP CONSTRAINT messages_status_check;
ALTER TABLE messages ADD CONSTRAINT messages_status_check CHECK(status IN ('generating','paused','complete','stopped','failed'));
DROP INDEX one_generating_answer;
CREATE UNIQUE INDEX one_generating_answer ON messages(conversation_id) WHERE status IN ('generating','paused');
