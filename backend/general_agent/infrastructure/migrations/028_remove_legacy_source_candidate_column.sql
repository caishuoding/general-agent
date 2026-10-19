-- 旧候选记忆流程已移除，来源删除不再保存候选标识。
ALTER TABLE source_deletions DROP COLUMN IF EXISTS candidate_ids;
