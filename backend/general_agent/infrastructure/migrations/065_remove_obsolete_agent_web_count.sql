-- 当前额度按 search_count 与 fetch_count 分类计算，web_count 没有写入方或读取方。
ALTER TABLE agents DROP COLUMN IF EXISTS web_count;
