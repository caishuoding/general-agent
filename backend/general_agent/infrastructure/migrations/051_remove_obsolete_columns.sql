-- 搜索确认流程已由模型工具取代，不再保存查询修订、外发说明或确认有效期。
ALTER TABLE search_requests DROP COLUMN IF EXISTS revision;
ALTER TABLE search_requests DROP COLUMN IF EXISTS purpose;
ALTER TABLE search_requests DROP COLUMN IF EXISTS expires_at;

-- 回答配置版本从未被读取；实际调用版本继续保存在 model_calls。
ALTER TABLE messages DROP COLUMN IF EXISTS model_revision;
ALTER TABLE messages DROP COLUMN IF EXISTS embedding_revision;

-- 图片描述已统一为纯文字；OCR 和旧结构字段不再产生或读取。
ALTER TABLE document_images DROP COLUMN IF EXISTS ocr_text;
ALTER TABLE document_images DROP COLUMN IF EXISTS uncertainties;
