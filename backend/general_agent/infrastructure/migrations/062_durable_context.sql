ALTER TABLE messages DROP CONSTRAINT messages_status_check;
ALTER TABLE messages ADD CONSTRAINT messages_status_check CHECK(status IN ('generating','complete','stopped','failed','interrupted'));
ALTER TABLE messages ADD COLUMN resume_model_revision TEXT;
ALTER TABLE messages ADD COLUMN interrupted_at TIMESTAMPTZ;
ALTER TABLE agent_runs ADD COLUMN paused_at TIMESTAMPTZ;
CREATE TABLE checkpoint_threads (
    thread_id TEXT PRIMARY KEY,
    conversation_id UUID NOT NULL,
    answer_id UUID NOT NULL
);
CREATE INDEX checkpoint_threads_conversation ON checkpoint_threads(conversation_id);
ALTER TABLE skill_commands ADD COLUMN tool_call_key TEXT;
CREATE UNIQUE INDEX skill_commands_call ON skill_commands(answer_id,tool_call_key) WHERE tool_call_key IS NOT NULL;
