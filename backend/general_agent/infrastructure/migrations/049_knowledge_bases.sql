-- 长期资料使用独立表，绝不继承会话附件的归属或 30 天有效期。
CREATE TABLE knowledge_bases (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    deleting BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX knowledge_bases_owner ON knowledge_bases(user_id);

CREATE TABLE knowledge_documents (
    id UUID PRIMARY KEY,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    size BIGINT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('processing','ready','failed','deleting')),
    stage TEXT NOT NULL DEFAULT 'queued',
    completed INTEGER NOT NULL DEFAULT 0,
    total INTEGER,
    error TEXT,
    warning TEXT,
    parse_info JSONB NOT NULL DEFAULT '{}',
    chunk_policy TEXT,
    index_revision TEXT,
    attempt UUID NOT NULL,
    operation_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX knowledge_documents_base ON knowledge_documents(knowledge_base_id);

CREATE TABLE knowledge_document_images (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    metadata JSONB NOT NULL,
    provider_file JSONB,
    vision_status TEXT NOT NULL CHECK(vision_status IN ('pending','complete','failed')),
    UNIQUE(document_id,ordinal),
    UNIQUE(document_id,id)
);
CREATE TABLE knowledge_chunks (
    document_id UUID NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    content TEXT NOT NULL,
    terms TEXT[] NOT NULL,
    page INTEGER,
    end_page INTEGER,
    paragraph INTEGER NOT NULL,
    end_paragraph INTEGER NOT NULL,
    structure JSONB NOT NULL DEFAULT '{}',
    end_structure JSONB NOT NULL DEFAULT '{}',
    context_prefix TEXT NOT NULL DEFAULT '',
    context_suffix TEXT NOT NULL DEFAULT '',
    token_count INTEGER,
    token_count_method TEXT,
    embedding JSONB,
    embedding_revision TEXT,
    image_id UUID,
    PRIMARY KEY(document_id,ordinal),
    FOREIGN KEY(document_id,image_id) REFERENCES knowledge_document_images(document_id,id) ON DELETE CASCADE
);
CREATE INDEX knowledge_chunks_terms ON knowledge_chunks USING GIN(terms);

CREATE TABLE conversation_knowledge_bases (
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    PRIMARY KEY(conversation_id,knowledge_base_id)
);
