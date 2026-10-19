# 任务 20：删除历史聊天检索

2026-10-19 按用户要求移除任务 16 的跨会话历史聊天检索。原聊天消息仍保存在 `messages` 表，长期记忆、附件和联网搜索保持原行为。

## 移除范围

- 删除聊天中的明确查询入口、历史检索模块和模型工具 `search_chat_history`。
- 删除消息响应的 `history_search` / `history_sources` 字段、历史出处界面和跳回原消息的前端状态。
- 新增 `030_remove_history_search.sql`，删除 `history_hits` 与 `history_queries`；旧迁移保留，确保已有数据库可按版本顺序升级。
- 2026-10-19 已直接对本地 `general_agent` 数据库执行该迁移：两张历史检索表计数为 0，迁移版本已记录，原有 4 条聊天消息仍保留。
- 删除历史检索专项后端与浏览器测试，更新提示词、工具集、对话图、来源删除及现行文档。

## 验证结果

- `.venv/bin/python -m pytest tests/memory/test_memory_deletion.py tests/memory/test_source_deletion.py tests/conversation/test_history_removed.py tests/app/test_prompts.py tests/memory/test_agent_tools.py tests/app/test_conversation_graph.py -q`：19 项通过。
- `tests/conversation/test_history_removed.py` 同时验证旧查询句式不再读取其他会话正文，以及迁移后历史表不存在。
- `.venv/bin/python -m compileall -q backend tests`、严格 mypy 和前端 TypeScript 类型检查通过；前端生产构建通过。
- 完整后端套件本轮为 81 项通过、36 项失败。失败集中在此轮并行进行的能力检测接口移除导致的 405 和依赖该接口的测试，与本任务修改的历史检索链路无关；未将完整套件标记为通过。
