ALTER TABLE agents ADD COLUMN model_limit INTEGER NOT NULL DEFAULT 96;
CREATE TABLE skill_tasks (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES users(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    skill TEXT NOT NULL,
    version TEXT NOT NULL,
    entry_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','waiting','completed','interrupted','revoked')),
    stage TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL DEFAULT '',
    instruction_files JSONB NOT NULL DEFAULT '{}',
    source_snapshot JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX skill_tasks_conversation ON skill_tasks(conversation_id,updated_at DESC);
CREATE TABLE skill_commands (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES skill_tasks(id) ON DELETE CASCADE,
    answer_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    command TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    exit_code INTEGER,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deadline TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX skill_command_running ON skill_commands(task_id) WHERE status='running';
CREATE TABLE skill_artifacts (
    id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES skill_tasks(id) ON DELETE CASCADE,
    answer_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    digest TEXT NOT NULL,
    media_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE skill_previews (
    token_digest TEXT PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES skill_tasks(id) ON DELETE CASCADE,
    port INTEGER NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
