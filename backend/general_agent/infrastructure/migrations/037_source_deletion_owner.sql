ALTER TABLE source_deletions ADD COLUMN user_id UUID REFERENCES users(id);
CREATE INDEX source_deletions_user_created ON source_deletions(user_id, created_at DESC, id);
