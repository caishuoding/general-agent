-- Agent 工具生成的查询词可能不同于用户原句，需单独保存以正确构造历史摘录。
ALTER TABLE history_queries ADD COLUMN IF NOT EXISTS query TEXT;
