-- 移除已停用的预算、价格和旧删除请求存储；当前用量只记录服务商实际返回值。
DROP TABLE IF EXISTS memory_delete_requests;
DROP TABLE IF EXISTS budget_settings;
DROP TABLE IF EXISTS model_prices;

-- 联网确认已改用 LangGraph 中断与 search_requests，旧预算暂停字段不再保存业务状态。
ALTER TABLE cloud_operations DROP COLUMN IF EXISTS approved;
ALTER TABLE cloud_operations DROP COLUMN IF EXISTS pause_id;
ALTER TABLE cloud_operations DROP COLUMN IF EXISTS reason;
ALTER TABLE messages DROP COLUMN IF EXISTS budget_pause;

-- 应用不再维护价格快照或费用估算，只保留实际 token、credits 与调用状态。
ALTER TABLE model_calls DROP COLUMN IF EXISTS estimated_cost;
ALTER TABLE model_calls DROP COLUMN IF EXISTS input_price;
ALTER TABLE model_calls DROP COLUMN IF EXISTS output_price;
ALTER TABLE model_calls DROP COLUMN IF EXISTS credit_price;

-- 新调用查询使用 model_calls_created，旧预算按月索引已重复。
DROP INDEX IF EXISTS model_calls_month;
