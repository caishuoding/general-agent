-- 原生 Word 结构与正文一起保存；旧 PDF/TXT 数据不修改，Word 在启动恢复时单独重建。
ALTER TABLE document_units ADD COLUMN structure JSONB NOT NULL DEFAULT '{}';
