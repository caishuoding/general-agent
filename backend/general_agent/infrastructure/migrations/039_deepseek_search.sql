-- 新联网请求使用 DeepSeek；历史记录保留当时实际使用的供应商说明。
ALTER TABLE search_requests ALTER COLUMN purpose SET DEFAULT '将查询或网页地址发送给 DeepSeek Search / Fetch，读取公开网页并回答本次问题。';

-- 搜索与抓取共享记录，单独标记搜索完成，避免抓取成功后误缓存先前失败的搜索。
ALTER TABLE search_requests ADD COLUMN search_completed BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE search_requests SET search_completed = TRUE WHERE status = 'complete';
