CREATE SEQUENCE web_source_keys;
CREATE TABLE search_requests (
    id UUID PRIMARY KEY,
    operation_id UUID UNIQUE NOT NULL REFERENCES cloud_operations(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    message_id UUID NOT NULL REFERENCES messages(id),
    query TEXT NOT NULL,
    search_revision TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('planning','pending','approved','complete','failed','cancelled','rejected','expired')),
    results JSONB NOT NULL DEFAULT '[]',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE messages ADD COLUMN web_sources JSONB NOT NULL DEFAULT '[]';
ALTER TABLE search_requests ADD COLUMN revision UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE search_requests ADD COLUMN sources JSONB NOT NULL DEFAULT '[]';
ALTER TABLE search_requests ADD COLUMN purpose TEXT NOT NULL DEFAULT '将这些具体文字发送给 Tavily，查询公开网页并回答本次问题。';
ALTER TABLE search_requests ADD COLUMN expires_at DOUBLE PRECISION NOT NULL DEFAULT 0;
