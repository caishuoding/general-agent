ALTER TABLE memory_actions DROP CONSTRAINT memory_actions_status_check;
ALTER TABLE memory_actions ADD CONSTRAINT memory_actions_status_check CHECK(status IN ('pending','saving','complete','failed','deleting','deleted'));
CREATE TABLE memory_deletions (
    action_id UUID PRIMARY KEY REFERENCES memory_actions(id),
    status TEXT NOT NULL CHECK(status IN ('pending','failed','complete')),
    memory_ids UUID[] NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE memory_updates DROP CONSTRAINT memory_updates_status_check;
ALTER TABLE memory_updates ADD CONSTRAINT memory_updates_status_check CHECK(status IN ('pending','saving','complete','failed','deleted'));
CREATE TABLE memory_source_suppressions (message_id UUID PRIMARY KEY);

ALTER TABLE cloud_operations ADD COLUMN invalidated BOOLEAN NOT NULL DEFAULT FALSE;
