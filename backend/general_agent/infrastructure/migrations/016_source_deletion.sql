ALTER TABLE conversations ADD COLUMN deleting BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE attachments DROP CONSTRAINT attachments_status_check;
ALTER TABLE attachments ADD CONSTRAINT attachments_status_check CHECK(status IN ('processing','ready','failed','deleting'));
ALTER TABLE memory_actions ALTER COLUMN message_id DROP NOT NULL;
ALTER TABLE memory_actions ALTER COLUMN conversation_id DROP NOT NULL;
ALTER TABLE memory_actions ADD COLUMN source_deleted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE memory_updates ADD COLUMN source_deleted BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE source_deletions (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('conversation','attachment')),
    source_id UUID NOT NULL,
    conversation_id UUID NOT NULL,
    revision TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','failed','complete')),
    phase TEXT NOT NULL,
    attachment_ids UUID[] NOT NULL,
    message_ids UUID[] NOT NULL,
    action_ids UUID[] NOT NULL,
    keep_ids UUID[] NOT NULL,
    operation_ids UUID[] NOT NULL DEFAULT '{}',
    job_ids UUID[] NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kind,source_id)
);
