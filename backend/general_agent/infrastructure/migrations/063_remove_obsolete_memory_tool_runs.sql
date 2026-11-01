-- AgentRuntime 的持久步骤已由 agent_messages 和 LangGraph checkpoint 承担。
-- 旧 MemoryTools.prepare 独立图已删除，该表不再有读取或写入方。
DROP TABLE IF EXISTS memory_tool_runs;
