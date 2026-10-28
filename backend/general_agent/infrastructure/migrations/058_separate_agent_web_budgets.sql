-- 每个智能体分别累计搜索与网页抓取的真实外发请求。
ALTER TABLE agents ADD COLUMN fetch_count INTEGER NOT NULL DEFAULT 0;
