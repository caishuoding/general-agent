-- 将搜索供应商切换为智谱，已有待确认卡片同步展示当前外发目的。
ALTER TABLE search_requests ALTER COLUMN purpose SET DEFAULT '将这些具体文字发送给智谱 Web Search，查询公开网页并回答本次问题。';
UPDATE search_requests
SET purpose = '将这些具体文字发送给智谱 Web Search，查询公开网页并回答本次问题。'
WHERE purpose = '将这些具体文字发送给 Tavily，查询公开网页并回答本次问题。';
