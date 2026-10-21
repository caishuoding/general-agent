-- 重叠切片可以跨段落与物理页，保存两端原文单元；已有索引在启动恢复时升级。
ALTER TABLE document_chunks ADD COLUMN end_unit_ordinal INTEGER;
UPDATE document_chunks SET end_unit_ordinal=unit_ordinal;
ALTER TABLE document_chunks ALTER COLUMN end_unit_ordinal SET NOT NULL;
ALTER TABLE document_chunks ADD FOREIGN KEY (attachment_id,end_unit_ordinal)
    REFERENCES document_units(attachment_id,ordinal) ON DELETE CASCADE;

-- 只清理旧版派生切片及其关键词索引；保留附件、原文和有效期供启动重建。
DELETE FROM document_chunks d USING attachments a
WHERE d.attachment_id=a.id AND a.knowledge_version<2;
