DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM conversations WHERE user_id IS NULL)
       OR EXISTS (SELECT 1 FROM model_calls WHERE user_id IS NULL)
       OR EXISTS (SELECT 1 FROM search_requests WHERE user_id IS NULL)
       OR EXISTS (SELECT 1 FROM memory_actions WHERE user_id IS NULL)
       OR EXISTS (SELECT 1 FROM cloud_operations WHERE user_id IS NULL)
       OR EXISTS (SELECT 1 FROM memory_recalls WHERE user_id IS NULL)
       OR EXISTS (SELECT 1 FROM source_deletions WHERE user_id IS NULL) THEN
        RAISE EXCEPTION '检测到未归属账号的旧业务数据，请先运行 scripts/auth_upgrade.py 备份并清空旧数据。';
    END IF;
END $$;

ALTER TABLE conversations ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE model_calls ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE search_requests ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE memory_actions ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE cloud_operations ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE memory_recalls ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE source_deletions ALTER COLUMN user_id SET NOT NULL;
