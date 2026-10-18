-- 项目只保留业务状态；Mem0 正文、向量、history 和旧确认候选不属于业务库。
DROP TABLE IF EXISTS attachment_memory_candidates;
DROP TABLE IF EXISTS memory_candidate_operations;
DROP TABLE IF EXISTS memory_discovery_operations;
DROP TABLE IF EXISTS memory_candidates;
DROP TABLE IF EXISTS memory_discoveries;
DROP TABLE IF EXISTS embedding_rebuild_operations;
DROP TABLE IF EXISTS embedding_rebuild_steps;
DROP TABLE IF EXISTS embedding_rebuilds;
DROP TABLE IF EXISTS embedding_spaces;
DROP TABLE IF EXISTS embedding_indexes;
DROP TABLE IF EXISTS mem0_history;
DROP TABLE IF EXISTS personal_memories;
DROP TABLE IF EXISTS memory_batches;
