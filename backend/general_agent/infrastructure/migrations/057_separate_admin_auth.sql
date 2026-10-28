-- 管理员凭据与登录会话迁出普通用户表；保留旧身份行仅供既有外键追溯。
CREATE TABLE admin_accounts (
    id UUID PRIMARY KEY,
    username TEXT NOT NULL UNIQUE DEFAULT 'admin' CHECK (username = 'admin'),
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin' CHECK (role = 'admin'),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status = 'active'),
    must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ,
    singleton BOOLEAN NOT NULL DEFAULT TRUE UNIQUE CHECK (singleton)
);

CREATE TABLE admin_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES admin_accounts(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX admin_sessions_user_id ON admin_sessions(user_id);
CREATE INDEX admin_sessions_expires_at ON admin_sessions(expires_at);

DO $$
BEGIN
    IF (SELECT count(*) FROM users WHERE role = 'admin') > 1 THEN
        RAISE EXCEPTION 'Only one administrator can be migrated';
    END IF;
END $$;

INSERT INTO admin_accounts
    (id, username, password_hash, role, status, must_change_password,
     created_at, updated_at, last_login_at, password_changed_at)
SELECT id, username, password_hash, role, 'active', must_change_password,
       created_at, updated_at, last_login_at, password_changed_at
FROM users WHERE role = 'admin';

-- 旧 Cookie 和旧会话在迁移后失效，用户表不再保留可用的管理员凭据。
DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE role = 'admin');
UPDATE users SET password_hash = 'migrated-to-admin-accounts', status = 'disabled'
WHERE role = 'admin';

ALTER TABLE admin_audit_log DROP CONSTRAINT admin_audit_log_admin_user_id_fkey;
ALTER TABLE admin_audit_log ADD CONSTRAINT admin_audit_log_admin_user_id_fkey
    FOREIGN KEY (admin_user_id) REFERENCES admin_accounts(id);
