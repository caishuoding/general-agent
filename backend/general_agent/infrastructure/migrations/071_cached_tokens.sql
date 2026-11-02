-- 未返回缓存用量及历史调用保留空值，不视为零命中。
ALTER TABLE model_calls ADD COLUMN cached_tokens INTEGER CHECK (cached_tokens >= 0);
