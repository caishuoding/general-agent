-- 搜索确认流程已删除，旧状态不会再产生；先归档为取消，再收紧为当前可达状态。
ALTER TABLE search_requests DROP CONSTRAINT IF EXISTS search_requests_status_check;
UPDATE search_requests
SET status='cancelled',
    error=coalesce(error, '旧版联网搜索确认流程已移除。')
WHERE status IN ('planning','pending','rejected','expired');
ALTER TABLE search_requests
ADD CONSTRAINT search_requests_status_check
CHECK(status IN ('approved','complete','failed','cancelled'));
