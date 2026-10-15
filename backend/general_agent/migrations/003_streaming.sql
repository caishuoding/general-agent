ALTER TABLE messages DROP CONSTRAINT messages_status_check;
ALTER TABLE messages ADD CONSTRAINT messages_status_check CHECK (status IN ('generating', 'complete', 'stopped', 'failed'));
ALTER TABLE messages ADD COLUMN reply_to UUID REFERENCES messages(id);
ALTER TABLE messages ADD COLUMN error TEXT;
ALTER TABLE model_calls DROP CONSTRAINT model_calls_status_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_status_check CHECK (status IN ('generating', 'complete', 'stopped', 'failed'));
ALTER TABLE model_calls ADD COLUMN assistant_message_id UUID REFERENCES messages(id);
CREATE UNIQUE INDEX one_generating_answer ON messages(conversation_id) WHERE status = 'generating';
