CREATE TABLE agents (
    id UUID PRIMARY KEY,
    answer_attempt_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES users(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    parent_agent_id UUID REFERENCES agents(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deadline TIMESTAMPTZ NOT NULL,
    closed BOOLEAN NOT NULL DEFAULT FALSE,
    model_count INTEGER NOT NULL DEFAULT 0,
    search_count INTEGER NOT NULL DEFAULT 0,
    source_snapshot JSONB NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX one_main_agent ON agents(answer_attempt_id) WHERE parent_agent_id IS NULL;
CREATE TABLE agent_runs (
    id UUID PRIMARY KEY,
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    operation_id UUID NOT NULL UNIQUE REFERENCES cloud_operations(id),
    request_key TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled','timed_out')),
    messages JSONB NOT NULL DEFAULT '[]',
    result TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    deadline TIMESTAMPTZ NOT NULL,
    UNIQUE(agent_id,sequence)
);
CREATE UNIQUE INDEX one_active_agent_run ON agent_runs(agent_id) WHERE status IN ('queued','running');
CREATE TABLE agent_messages (
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY(run_id,step_key)
);
CREATE TABLE agent_sources (
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    delivered BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY(run_id,source_key)
);
CREATE TABLE agent_tool_requests (
    parent_run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    request_key TEXT NOT NULL,
    result JSONB NOT NULL,
    PRIMARY KEY(parent_run_id,request_key)
);
ALTER TABLE model_calls ADD COLUMN agent_id UUID REFERENCES agents(id) ON DELETE SET NULL;
ALTER TABLE model_calls ADD COLUMN agent_run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL;
CREATE INDEX agent_runs_operation ON agent_runs(operation_id);
