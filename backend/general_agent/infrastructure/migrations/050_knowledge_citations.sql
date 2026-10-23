-- 知识库引用由回答尝试保存，编号与网页来源分开命名以免混淆。
CREATE SEQUENCE knowledge_source_keys;
ALTER TABLE messages ADD COLUMN knowledge_sources JSONB NOT NULL DEFAULT '[]';
