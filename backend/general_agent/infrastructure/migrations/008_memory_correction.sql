CREATE TABLE memory_updates (
    id UUID PRIMARY KEY,
    action_id UUID NOT NULL REFERENCES memory_actions(id),
    operation_id UUID NOT NULL REFERENCES cloud_operations(id),
    target_version INTEGER NOT NULL,
    previous_fact TEXT NOT NULL,
    fact TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','saving','complete','failed')),
    error TEXT,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('management','chat')),
    source_message_id UUID REFERENCES messages(id),
    source_conversation_id UUID REFERENCES conversations(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(action_id,target_version)
);
CREATE INDEX memory_updates_source_message ON memory_updates(source_message_id);
CREATE TABLE memory_update_operations (
    update_id UUID NOT NULL REFERENCES memory_updates(id),
    operation_id UUID PRIMARY KEY REFERENCES cloud_operations(id)
);
