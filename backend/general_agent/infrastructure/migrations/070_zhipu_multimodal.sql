-- 智谱 GLM-5.3-Flash 直接接收本地原图；移除旧文件上传身份。
ALTER TABLE attachments DROP COLUMN IF EXISTS provider_file;
ALTER TABLE document_images DROP COLUMN IF EXISTS provider_file;
ALTER TABLE knowledge_document_images DROP COLUMN IF EXISTS provider_file;
