-- 附件只保留原文件与完整可读文字；旧的切块检索和文档总结状态全部移除。
ALTER TABLE attachments ADD COLUMN IF NOT EXISTS content TEXT;

-- 已有附件先把旧切块合并成完整文字，避免升级后历史资料突然不可用。
UPDATE attachments AS attachment
SET content = source.content
FROM (
    SELECT
        attachment_id,
        string_agg('[' || location || E']\n' || content, E'\n\n' ORDER BY ordinal) AS content
    FROM attachment_chunks
    GROUP BY attachment_id
) AS source
WHERE attachment.id = source.attachment_id
  AND attachment.image_metadata IS NULL
  AND attachment.content IS NULL;

ALTER TABLE messages DROP COLUMN IF EXISTS citations;
ALTER TABLE messages DROP COLUMN IF EXISTS document_progress;
ALTER TABLE messages DROP COLUMN IF EXISTS document_job_id;
DROP TABLE IF EXISTS document_steps;
DROP TABLE IF EXISTS document_jobs;
ALTER TABLE source_deletions DROP COLUMN IF EXISTS job_ids;
DROP TABLE IF EXISTS message_attachments;
DROP TABLE IF EXISTS attachment_chunks;
