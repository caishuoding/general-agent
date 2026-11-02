-- Skill 加载无状态；仅保留会话已用名称、按 Run 隔离的命令、生成文件和预览。
DROP TABLE IF EXISTS skill_previews;
DROP TABLE IF EXISTS skill_artifacts;
DROP TABLE IF EXISTS skill_commands;
DROP TABLE IF EXISTS skill_tasks;

ALTER TABLE messages DROP COLUMN IF EXISTS skill_selections;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS skill_names JSONB NOT NULL DEFAULT '[]';

CREATE TABLE conversation_skills (
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    first_used_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (conversation_id, skill_name)
);

CREATE TABLE generated_files (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    created_by_run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    digest TEXT NOT NULL,
    media_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX generated_files_conversation ON generated_files(conversation_id,created_at DESC);

CREATE TABLE skill_commands (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    command TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    exit_code INTEGER,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deadline TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    tool_call_key TEXT
);
CREATE INDEX skill_commands_run ON skill_commands(run_id,created_at DESC);
CREATE UNIQUE INDEX skill_command_call ON skill_commands(run_id,tool_call_key) WHERE tool_call_key IS NOT NULL;
CREATE UNIQUE INDEX skill_command_running ON skill_commands(run_id) WHERE status='running';

CREATE TABLE skill_previews (
    token_digest TEXT PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    port INTEGER NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX skill_previews_run ON skill_previews(run_id,expires_at);
