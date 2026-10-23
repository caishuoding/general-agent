-- 当前对话图不写检查点；旧 langgraph schema 不再承担恢复或业务存储职责。
DROP SCHEMA IF EXISTS langgraph CASCADE;
