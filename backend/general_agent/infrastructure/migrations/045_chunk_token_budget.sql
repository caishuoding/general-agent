-- Markdown 重复表头与单元格续片包装不是原文偏移的一部分；切片记录实际或估算 Token 数。
ALTER TABLE document_chunks ADD COLUMN context_prefix TEXT NOT NULL DEFAULT '';
ALTER TABLE document_chunks ADD COLUMN context_suffix TEXT NOT NULL DEFAULT '';
ALTER TABLE document_chunks ADD COLUMN token_count INTEGER CHECK (token_count>=0);
ALTER TABLE document_chunks ADD COLUMN token_count_method TEXT;
