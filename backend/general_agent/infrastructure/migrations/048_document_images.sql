-- 文档内嵌图片保存稳定身份、位置和结构化视觉描述；图片切片可在召回时还原原图。
CREATE TABLE document_images (
    id UUID PRIMARY KEY,
    attachment_id UUID NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    page INTEGER,
    end_page INTEGER,
    paragraph INTEGER NOT NULL,
    section JSONB NOT NULL DEFAULT '[]',
    location TEXT NOT NULL,
    storage_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    ocr_text TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    uncertainties JSONB NOT NULL DEFAULT '[]',
    provider_file JSONB,
    vision_status TEXT NOT NULL CHECK (vision_status IN ('pending','complete','failed')),
    vision_model TEXT,
    vision_revision TEXT,
    vision_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(attachment_id, ordinal)
);

CREATE INDEX document_images_attachment ON document_images(attachment_id, page, ordinal);

ALTER TABLE document_chunks ADD COLUMN image_id UUID;
ALTER TABLE document_chunks ADD CONSTRAINT document_chunk_image
    FOREIGN KEY (image_id) REFERENCES document_images(id) ON DELETE CASCADE;
