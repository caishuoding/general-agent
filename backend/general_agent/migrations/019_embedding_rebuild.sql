CREATE TABLE embedding_spaces (
    id UUID PRIMARY KEY,
    collection_name TEXT UNIQUE NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK(dimensions>0),
    status TEXT NOT NULL CHECK(status IN ('active','target','retired','removed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX one_active_memory_space ON embedding_spaces(status) WHERE status='active';
CREATE TABLE embedding_rebuilds (
    id UUID PRIMARY KEY,
    scope_hash TEXT NOT NULL,
    target_id UUID NOT NULL REFERENCES embedding_spaces(id),
    old_id UUID REFERENCES embedding_spaces(id),
    target_revision TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'initializing',
    status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','paused','failed','complete','cancelling','cancelled')),
    initialized BOOLEAN NOT NULL DEFAULT FALSE,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    pause JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX one_open_memory_rebuild ON embedding_rebuilds((TRUE)) WHERE status NOT IN ('complete','cancelled');
CREATE TABLE embedding_rebuild_steps (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES embedding_rebuilds(id),
    action_id UUID REFERENCES memory_actions(id),
    memory_id UUID,
    version INTEGER,
    fact_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','complete','invalidated')),
    operation_id UUID,
    call_id UUID,
    UNIQUE(job_id,action_id)
);
CREATE TABLE embedding_rebuild_operations (
    step_id UUID NOT NULL REFERENCES embedding_rebuild_steps(id),
    operation_id UUID NOT NULL REFERENCES cloud_operations(id),
    PRIMARY KEY(step_id,operation_id)
);
