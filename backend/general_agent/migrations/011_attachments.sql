CREATE TABLE attachments (
    id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    filename TEXT NOT NULL,
    size BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'ready', 'failed')),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX attachments_conversation ON attachments(conversation_id);
CREATE TABLE attachment_chunks (
    citation_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    attachment_id UUID NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    location TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (attachment_id, ordinal)
);
CREATE TABLE message_attachments (
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    attachment_id UUID NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, attachment_id)
);
ALTER TABLE messages ADD COLUMN citations JSONB NOT NULL DEFAULT '[]';
