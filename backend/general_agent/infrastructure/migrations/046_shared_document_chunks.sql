-- 阅读与检索共用完整切片；先迁移位置和结构，再删除重复正文与原文外键。
ALTER TABLE document_chunks ADD COLUMN page INTEGER;
ALTER TABLE document_chunks ADD COLUMN end_page INTEGER;
ALTER TABLE document_chunks ADD COLUMN paragraph INTEGER;
ALTER TABLE document_chunks ADD COLUMN end_paragraph INTEGER;
ALTER TABLE document_chunks ADD COLUMN structure JSONB NOT NULL DEFAULT '{}';
ALTER TABLE document_chunks ADD COLUMN end_structure JSONB NOT NULL DEFAULT '{}';

UPDATE document_chunks d SET
    page=u.page,
    end_page=COALESCE((e.structure->>'end_page')::int,e.page),
    paragraph=u.paragraph,
    end_paragraph=e.paragraph,
    structure=u.structure,
    end_structure=e.structure
FROM document_units u, document_units e
WHERE u.attachment_id=d.attachment_id AND u.ordinal=d.unit_ordinal
  AND e.attachment_id=d.attachment_id AND e.ordinal=d.end_unit_ordinal;

ALTER TABLE document_chunks ALTER COLUMN paragraph SET NOT NULL;
ALTER TABLE document_chunks ALTER COLUMN end_paragraph SET NOT NULL;
ALTER TABLE document_chunks DROP COLUMN unit_ordinal;
ALTER TABLE document_chunks DROP COLUMN end_unit_ordinal;
ALTER TABLE document_chunks DROP COLUMN start_offset;
ALTER TABLE document_chunks DROP COLUMN end_offset;
DROP TABLE document_units;
ALTER TABLE attachments DROP COLUMN content;
