CREATE TABLE cloud_operations (
    id UUID PRIMARY KEY,
    scope_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    conversation_id UUID REFERENCES conversations(id),
    cancelled BOOLEAN NOT NULL DEFAULT FALSE,
    result JSONB,
    response_status INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE model_calls DROP CONSTRAINT model_calls_status_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_status_check CHECK(status IN ('reserved','generating','complete','stopped','failed','cancelled'));
ALTER TABLE model_calls ADD COLUMN operation_id UUID REFERENCES cloud_operations(id);
ALTER TABLE model_calls ADD COLUMN base_url TEXT;
ALTER TABLE model_calls ADD COLUMN revision TEXT;
ALTER TABLE model_calls ADD COLUMN unit TEXT NOT NULL DEFAULT 'chat_tokens';
ALTER TABLE model_calls ADD COLUMN credits NUMERIC(24,12);
ALTER TABLE model_calls ADD COLUMN finished_at TIMESTAMPTZ;
UPDATE model_calls SET finished_at = created_at WHERE status <> 'generating';
CREATE INDEX model_calls_created ON model_calls(created_at);
