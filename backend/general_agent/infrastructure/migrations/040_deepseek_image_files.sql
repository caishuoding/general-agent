-- 图片保存远端文件身份与到期时间，不再向聊天请求重复发送 base64 原图。
ALTER TABLE attachments ADD COLUMN provider_file JSONB;
