ALTER TABLE memory_candidates ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'message' CHECK(source_kind IN ('message','attachment'));
CREATE TABLE attachment_memory_candidates (
    candidate_id UUID PRIMARY KEY REFERENCES memory_candidates(id) ON DELETE CASCADE,
    attachment_id UUID NOT NULL REFERENCES attachments(id),
    source_version TIMESTAMPTZ NOT NULL,
    citation_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    location TEXT NOT NULL,
    extracted_fact TEXT NOT NULL,
    suggested_subject TEXT NOT NULL,
    subject_label TEXT,
    is_image BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX attachment_memory_candidates_source ON attachment_memory_candidates(attachment_id);
