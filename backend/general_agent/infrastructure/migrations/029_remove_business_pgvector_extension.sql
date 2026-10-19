-- 业务库不再保存向量；pgvector 只由独立 Mem0 PostgreSQL 使用。
DROP EXTENSION IF EXISTS vector;
