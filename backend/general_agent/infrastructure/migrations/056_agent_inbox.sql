CREATE TABLE agent_inbox_deliveries (
    id UUID PRIMARY KEY,
    recipient_run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    delivery_key TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('automatic','wait','settle')),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(recipient_run_id,delivery_key)
);
CREATE TABLE agent_inbox_events (
    id BIGSERIAL PRIMARY KEY,
    recipient_run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    sender_run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    delivery_id UUID REFERENCES agent_inbox_deliveries(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(recipient_run_id,sender_run_id)
);
CREATE INDEX agent_inbox_unread ON agent_inbox_events(recipient_run_id,id) WHERE delivery_id IS NULL;
