# 使用 Mem0 实现长期记忆

状态：自动记忆方式已由 [ADR 0005](0005-native-automatic-memory.md) 取代；存储实现已改为独立官方 Mem0 Docker 服务。

长期记忆采用用户明确指定的 Mem0。Mem0 作为独立 Docker 服务运行，使用自己的 PostgreSQL/pgvector 和内部 history；业务后端只调用官方 REST API，业务 PostgreSQL 仅保存来源、动作、删除墓碑和恢复检查点，不复制记忆正文或向量，也不实现 SDK、history 或 pgvector 适配层。

Mem0 只处理本次获准的用户输入，不能将助手回答、历史聊天或附件正文写入长期记忆。应用负责人物归属、来源关联、幂等动作和删除后防止旧来源自动恢复；会话资料和历史消息检索保持独立边界。
