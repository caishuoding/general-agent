-- 附件向量属于会话切片，删除切片即删除缓存；版本变化时按需重算。
ALTER TABLE document_chunks ADD COLUMN embedding JSONB;
ALTER TABLE document_chunks ADD COLUMN embedding_revision TEXT;
ALTER TABLE document_chunks ADD CONSTRAINT document_embedding_pair
    CHECK ((embedding IS NULL) = (embedding_revision IS NULL));
