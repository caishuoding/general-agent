CREATE TABLE budget_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    monthly_limit NUMERIC(24,12) CHECK(monthly_limit >= 0)
);
INSERT INTO budget_settings(singleton) VALUES(TRUE);
CREATE TABLE model_prices (
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    input_per_million NUMERIC(24,12),
    output_per_million NUMERIC(24,12),
    per_credit NUMERIC(24,12),
    PRIMARY KEY(base_url, model)
);
CREATE TABLE cloud_operations (
    id UUID PRIMARY KEY,
    scope_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    conversation_id UUID REFERENCES conversations(id),
    cancelled BOOLEAN NOT NULL DEFAULT FALSE,
    approved BOOLEAN NOT NULL DEFAULT FALSE,
    pause_id UUID,
    reason TEXT,
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
ALTER TABLE model_calls ADD COLUMN estimated_cost NUMERIC(38,12);
ALTER TABLE model_calls ADD COLUMN input_price NUMERIC(24,12);
ALTER TABLE model_calls ADD COLUMN output_price NUMERIC(24,12);
ALTER TABLE model_calls ADD COLUMN credit_price NUMERIC(24,12);
ALTER TABLE model_calls ADD COLUMN finished_at TIMESTAMPTZ;
UPDATE model_calls SET finished_at = created_at WHERE status <> 'generating';
CREATE INDEX model_calls_month ON model_calls(created_at);
