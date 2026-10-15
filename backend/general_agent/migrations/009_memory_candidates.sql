CREATE TABLE memory_discoveries (
    message_id UUID PRIMARY KEY REFERENCES messages(id),
    operation_id UUID NOT NULL REFERENCES cloud_operations(id),
    status TEXT NOT NULL CHECK(status IN ('pending','complete','failed')),
    error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE memory_candidates (
    id UUID PRIMARY KEY,
    source_message_id UUID NOT NULL REFERENCES messages(id),
    source_conversation_id UUID NOT NULL REFERENCES conversations(id),
    source_quote TEXT NOT NULL,
    fact TEXT NOT NULL,
    subject TEXT NOT NULL,
    target_id UUID REFERENCES memory_actions(id),
    target_version INTEGER,
    target_fact TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','saving','paused','failed','confirmed','rejected','expired')),
    error TEXT,
    approved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP + INTERVAL '7 days',
    memory_action_id UUID REFERENCES memory_actions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX memory_candidates_source ON memory_candidates(source_message_id);
CREATE TABLE memory_candidate_operations (
    candidate_id UUID NOT NULL REFERENCES memory_candidates(id),
    operation_id UUID PRIMARY KEY REFERENCES cloud_operations(id)
);

ALTER TABLE memory_actions DROP CONSTRAINT memory_actions_message_id_key;
CREATE INDEX memory_actions_source_message ON memory_actions(message_id);
ALTER TABLE memory_updates DROP CONSTRAINT memory_updates_source_message_id_key;
CREATE INDEX memory_updates_source_message ON memory_updates(source_message_id);
CREATE TABLE memory_discovery_operations (
    message_id UUID NOT NULL REFERENCES messages(id),
    operation_id UUID PRIMARY KEY REFERENCES cloud_operations(id)
);
