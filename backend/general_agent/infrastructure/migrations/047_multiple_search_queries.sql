-- 每个回答可执行多个查询；按查询保存完成状态，空结果也能独立复用。
ALTER TABLE search_requests ADD COLUMN completed_queries TEXT[] NOT NULL DEFAULT '{}';
UPDATE search_requests SET completed_queries = ARRAY[query]
WHERE search_completed AND query <> '';

-- 旧搜索结果补上实际查询，避免多个查询之间串用缓存；抓取结果保持原样。
UPDATE search_requests s SET results = (
    SELECT jsonb_agg(
        CASE WHEN COALESCE(item->>'kind', 'search') = 'search'
             THEN item || jsonb_build_object('query', s.query) ELSE item END
        ORDER BY position
    )
    FROM jsonb_array_elements(s.results) WITH ORDINALITY AS entries(item, position)
)
WHERE jsonb_array_length(results) > 0;

ALTER TABLE search_requests DROP COLUMN search_completed;
