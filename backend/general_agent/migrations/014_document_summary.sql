CREATE TABLE document_jobs (
    id UUID PRIMARY KEY,
    user_message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    model_revision TEXT NOT NULL,
    mode TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    attachment_ids UUID[] NOT NULL,
    files JSONB NOT NULL,
    total_steps INTEGER NOT NULL,
    final_content TEXT,
    final_call_id UUID REFERENCES model_calls(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_message_id,model_revision)
);
CREATE INDEX document_jobs_attachments ON document_jobs USING GIN(attachment_ids);
CREATE TABLE document_steps (
    job_id UUID NOT NULL REFERENCES document_jobs(id) ON DELETE CASCADE,
    level INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
    content TEXT,
    call_id UUID REFERENCES model_calls(id),
    PRIMARY KEY(job_id,level,ordinal)
);
ALTER TABLE messages ADD COLUMN document_job_id UUID REFERENCES document_jobs(id) ON DELETE SET NULL;
ALTER TABLE messages ADD COLUMN document_progress JSONB;
