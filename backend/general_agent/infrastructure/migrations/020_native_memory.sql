CREATE TABLE memory_native_runs (
    message_id UUID PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    operation_id UUID NOT NULL REFERENCES cloud_operations(id),
    status TEXT NOT NULL CHECK(status IN ('pending','complete','failed','unavailable','skipped')),
    detail TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE memory_native_steps (
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    step_key TEXT NOT NULL,
    result JSONB NOT NULL,
    PRIMARY KEY(message_id,step_key)
);
CREATE TABLE memory_tool_runs (
    message_id UUID PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    operation_id UUID NOT NULL REFERENCES cloud_operations(id),
    status TEXT NOT NULL CHECK(status IN ('pending','complete')),
    result JSONB NOT NULL DEFAULT '{}'
);
CREATE TABLE memory_tool_observations (
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    action_id UUID NOT NULL REFERENCES memory_actions(id),
    version INTEGER NOT NULL,
    PRIMARY KEY(message_id,action_id)
);
