-- 会话文档原文与检索索引独立于长期记忆，旧附件由启动恢复从原件重建。
ALTER TABLE attachments ADD COLUMN expires_at TIMESTAMPTZ;
UPDATE attachments SET expires_at=created_at + INTERVAL '30 days';
ALTER TABLE attachments ALTER COLUMN expires_at SET DEFAULT (CURRENT_TIMESTAMP + INTERVAL '30 days');
ALTER TABLE attachments ALTER COLUMN expires_at SET NOT NULL;
ALTER TABLE attachments ADD COLUMN knowledge_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE attachments ADD COLUMN expired_cleaned BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX attachments_expiration ON attachments(expires_at) WHERE NOT expired_cleaned;

CREATE TABLE document_units (
    attachment_id UUID NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    page INTEGER,
    paragraph INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (attachment_id, ordinal)
);
CREATE TABLE document_chunks (
    attachment_id UUID NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    unit_ordinal INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    content TEXT NOT NULL,
    terms TEXT[] NOT NULL,
    PRIMARY KEY (attachment_id, ordinal),
    FOREIGN KEY (attachment_id, unit_ordinal) REFERENCES document_units(attachment_id, ordinal) ON DELETE CASCADE
);
CREATE INDEX document_chunks_terms ON document_chunks USING GIN(terms);
