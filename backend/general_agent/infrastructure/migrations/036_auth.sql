CREATE TABLE users (
    id UUID PRIMARY KEY,
    username TEXT NOT NULL CHECK (username = lower(username)),
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ,
    CONSTRAINT users_username_format CHECK (username ~ '^[a-z0-9_]{3,32}$')
);
CREATE UNIQUE INDEX users_username_unique ON users(username);

CREATE TABLE sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX sessions_user_id ON sessions(user_id);
CREATE INDEX sessions_expires_at ON sessions(expires_at);

CREATE TABLE admin_audit_log (
    id UUID PRIMARY KEY,
    admin_user_id UUID NOT NULL REFERENCES users(id),
    target_user_id UUID REFERENCES users(id),
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    action TEXT NOT NULL,
    ip_address TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX admin_audit_log_admin_created ON admin_audit_log(admin_user_id, created_at DESC);
CREATE INDEX admin_audit_log_target_created ON admin_audit_log(target_user_id, created_at DESC);

ALTER TABLE conversations ADD COLUMN user_id UUID REFERENCES users(id);
ALTER TABLE model_calls ADD COLUMN user_id UUID REFERENCES users(id);
ALTER TABLE search_requests ADD COLUMN user_id UUID REFERENCES users(id);
ALTER TABLE memory_actions ADD COLUMN user_id UUID REFERENCES users(id);
ALTER TABLE cloud_operations ADD COLUMN user_id UUID REFERENCES users(id);
ALTER TABLE memory_recalls ADD COLUMN user_id UUID REFERENCES users(id);

CREATE INDEX conversations_user_created ON conversations(user_id, created_at DESC, id);
CREATE INDEX model_calls_user_created ON model_calls(user_id, created_at DESC, id);
CREATE INDEX search_requests_user_created ON search_requests(user_id, created_at DESC, id);
CREATE INDEX memory_actions_user_created ON memory_actions(user_id, created_at DESC, id);
CREATE INDEX cloud_operations_user_created ON cloud_operations(user_id, created_at DESC, id);
CREATE INDEX memory_recalls_user_created ON memory_recalls(user_id, created_at DESC, operation_id);
