-- 搜索与网页抓取共用每个智能体的联网调用额度，并保留旧分类计数供历史追溯。
ALTER TABLE agents ADD COLUMN web_count INTEGER NOT NULL DEFAULT 0;
UPDATE agents SET web_count = search_count + fetch_count;
