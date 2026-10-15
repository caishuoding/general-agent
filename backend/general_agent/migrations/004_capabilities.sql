ALTER TABLE model_calls ALTER COLUMN conversation_id DROP NOT NULL;
ALTER TABLE model_calls ADD COLUMN purpose TEXT NOT NULL DEFAULT 'chat';
CREATE TABLE capability_checks (
    capability TEXT NOT NULL CHECK (capability IN ('text', 'image', 'tools', 'embedding')),
    revision TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    detail TEXT NOT NULL,
    dimensions INTEGER,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (capability, revision)
);
CREATE TABLE embedding_indexes (
    name TEXT PRIMARY KEY,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
